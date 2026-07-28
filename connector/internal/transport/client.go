package transport

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
	"github.com/adx-agentic-payment/adx/connector/internal/store"
	"github.com/adx-agentic-payment/adx/connector/internal/supervisor"
	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

const (
	maxMessageBytes = 1024 * 1024
	replayWindow    = 64
	replayBurst     = 8
)

const (
	messageWelcome = "welcome"
	messageAck     = "ack"
	messageError   = "error"
)

var ErrConnectionReplaced = errors.New("connector connection was replaced by another active instance")
var ErrDeviceRevoked = errors.New("connector device was revoked")
var ErrPersistenceDegraded = errors.New("connector local persistence is degraded")

type inventoryWire struct {
	ObservedAt time.Time         `json:"observed_at"`
	Host       protocol.HostInfo `json:"host"`
	Runtimes   []runtimeWire     `json:"runtimes"`
}

type runtimeWire struct {
	RuntimeID            string    `json:"runtime_id"`
	Kind                 string    `json:"kind"`
	DisplayName          string    `json:"display_name"`
	ExecutablePath       string    `json:"executable_path"`
	Version              string    `json:"version,omitempty"`
	Available            bool      `json:"available"`
	Capabilities         []string  `json:"capabilities"`
	AuthModes            []string  `json:"auth_modes"`
	TaskEnabled          bool      `json:"task_enabled"`
	AuthenticationStatus string    `json:"authentication_status"`
	ArenaCompatible      bool      `json:"arena_compatible"`
	ArenaIsolation       string    `json:"arena_isolation"`
	LocalExecutionReady  bool      `json:"local_execution_ready"`
	ReadinessIssues      []string  `json:"readiness_issues,omitempty"`
	DetectedAt           time.Time `json:"detected_at"`
}

type commandAckWire struct {
	CommandID    string         `json:"command_id"`
	BindingEpoch uint64         `json:"binding_epoch"`
	Status       string         `json:"status"`
	Result       map[string]any `json:"result,omitempty"`
	Error        map[string]any `json:"error,omitempty"`
}

type runtimeEventWire struct {
	EventID    string         `json:"event_id"`
	BindingID  string         `json:"binding_id"`
	SessionID  string         `json:"session_id,omitempty"`
	TaskID     string         `json:"task_id,omitempty"`
	Sequence   uint64         `json:"sequence"`
	EventType  string         `json:"event_type"`
	OccurredAt time.Time      `json:"occurred_at"`
	Level      string         `json:"level"`
	Data       map[string]any `json:"data"`
}

type StateStore interface {
	StageEvent(protocol.RuntimeEvent) (protocol.RuntimeEvent, error)
	StagedEvent() (*protocol.RuntimeEvent, error)
	ClearStagedEvent(uint64) error
	Receipts() ([]protocol.CommandAck, error)
	RecoverInterruptedReceipts() (int, error)
	AgentTaskResults() ([]protocol.AgentTaskResultEnvelope, error)
	AckAgentTaskResult(string, string) error
}

type Outbox interface {
	Append(protocol.RuntimeEvent) error
	Pending() ([]protocol.RuntimeEvent, error)
	AckThrough(uint64) error
}

type Config struct {
	Credentials       store.Credentials
	ConnectorVersion  string
	HeartbeatInterval time.Duration
	InventoryInterval time.Duration
	ReconnectMin      time.Duration
	ReconnectMax      time.Duration
}

type Client struct {
	config           Config
	state            StateStore
	outbox           Outbox
	supervisor       *supervisor.Supervisor
	logger           *log.Logger
	startedAt        time.Time
	persistenceMu    sync.RWMutex
	persistenceFault error
}

type eventReplay struct {
	events       []protocol.RuntimeEvent
	next         int
	inFlight     map[uint64]struct{}
	highestSent  uint64
	lastSequence uint64
}

func newEventReplay(events []protocol.RuntimeEvent) (*eventReplay, error) {
	for index := 1; index < len(events); index++ {
		if events[index].Sequence != events[index-1].Sequence+1 {
			return nil, fmt.Errorf(
				"event outbox has a sequence gap between %d and %d",
				events[index-1].Sequence,
				events[index].Sequence,
			)
		}
	}
	replay := &eventReplay{
		events:   append([]protocol.RuntimeEvent(nil), events...),
		inFlight: make(map[uint64]struct{}),
	}
	if len(events) != 0 {
		replay.lastSequence = events[len(events)-1].Sequence
	}
	return replay, nil
}

func (r *eventReplay) Add(event protocol.RuntimeEvent) error {
	if event.Sequence == 0 {
		return errors.New("replay event sequence is required")
	}
	if r.lastSequence != 0 && event.Sequence != r.lastSequence+1 {
		return fmt.Errorf(
			"replay event sequence %d does not follow %d",
			event.Sequence,
			r.lastSequence,
		)
	}
	r.events = append(r.events, event)
	r.lastSequence = event.Sequence
	return nil
}

func (r *eventReplay) Next() (protocol.RuntimeEvent, bool) {
	if r.next >= len(r.events) || len(r.inFlight) >= replayWindow {
		return protocol.RuntimeEvent{}, false
	}
	return r.events[r.next], true
}

func (r *eventReplay) MarkSent(event protocol.RuntimeEvent) {
	r.next++
	r.inFlight[event.Sequence] = struct{}{}
	if event.Sequence > r.highestSent {
		r.highestSent = event.Sequence
	}
}

func (r *eventReplay) CanAcknowledge(sequence uint64) bool {
	return sequence <= r.highestSent
}

func (r *eventReplay) AcknowledgeThrough(sequence uint64) {
	for sent := range r.inFlight {
		if sent <= sequence {
			delete(r.inFlight, sent)
		}
	}
	remove := 0
	for remove < r.next && r.events[remove].Sequence <= sequence {
		remove++
	}
	if remove != 0 {
		r.events = r.events[remove:]
		r.next -= remove
	}
}

func NewClient(
	config Config,
	state StateStore,
	outbox Outbox,
	processSupervisor *supervisor.Supervisor,
	logger *log.Logger,
) (*Client, error) {
	if err := config.Credentials.Validate(); err != nil {
		return nil, err
	}
	if _, err := validateGatewayURL(config.Credentials.GatewayURL); err != nil {
		return nil, err
	}
	if state == nil || outbox == nil || processSupervisor == nil {
		return nil, errors.New("state store, outbox, and supervisor are required")
	}
	if logger == nil {
		logger = log.New(io.Discard, "", 0)
	}
	if config.HeartbeatInterval <= 0 {
		config.HeartbeatInterval = 15 * time.Second
	}
	if config.InventoryInterval <= 0 {
		config.InventoryInterval = time.Minute
	}
	if config.ReconnectMin <= 0 {
		config.ReconnectMin = time.Second
	}
	if config.ReconnectMax <= 0 {
		config.ReconnectMax = 30 * time.Second
	}
	return &Client{
		config:     config,
		state:      state,
		outbox:     outbox,
		supervisor: processSupervisor,
		logger:     logger,
		startedAt:  time.Now().UTC(),
	}, nil
}

func (c *Client) Run(ctx context.Context) error {
	recovered, err := c.state.RecoverInterruptedReceipts()
	if err != nil {
		return fmt.Errorf("recover interrupted command receipts: %w", err)
	}
	if recovered != 0 {
		c.logger.Printf("recovered %d interrupted command receipt(s)", recovered)
	}
	if err := c.recoverStagedEvent(); err != nil {
		return fmt.Errorf("recover staged runtime event: %w", err)
	}

	backoff := c.config.ReconnectMin
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		err := c.runConnection(ctx)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if errors.Is(err, ErrPersistenceDegraded) {
			return err
		}
		if websocket.CloseStatus(err) == websocket.StatusCode(4403) {
			return fmt.Errorf("%w: %v", ErrDeviceRevoked, err)
		}
		if websocket.CloseStatus(err) == websocket.StatusCode(4409) {
			return fmt.Errorf("%w: %v", ErrConnectionReplaced, err)
		}
		c.logger.Printf("connector disconnected: %v; reconnecting", err)

		wait := jitter(backoff)
		timer := time.NewTimer(wait)
		for {
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case event := <-c.supervisor.Events():
				if persistErr := c.persistEvent(&event); persistErr != nil {
					timer.Stop()
					return persistErr
				}
			case <-c.supervisor.Results():
				// AgentTask results are already durable. The next connection
				// replays them from the state store.
			case <-timer.C:
				goto reconnect
			}
		}
	reconnect:
		backoff *= 2
		if backoff > c.config.ReconnectMax {
			backoff = c.config.ReconnectMax
		}
	}
}

func (c *Client) runConnection(ctx context.Context) error {
	if err := c.persistenceDegraded(); err != nil {
		return err
	}
	if err := c.recoverStagedEvent(); err != nil {
		return fmt.Errorf("recover staged runtime event before reconnect: %w", err)
	}
	endpoint, err := gatewayEndpoint(c.config.Credentials.GatewayURL, c.config.Credentials.DeviceID)
	if err != nil {
		return err
	}
	headers := make(http.Header)
	headers.Set("Authorization", "Device "+c.config.Credentials.Token)
	headers.Set("X-ADX-Protocol-Version", protocol.Version)
	connection, response, err := websocket.Dial(ctx, endpoint, &websocket.DialOptions{
		HTTPHeader: headers,
	})
	if err != nil {
		if response != nil {
			_ = response.Body.Close()
			return fmt.Errorf("gateway websocket handshake returned HTTP %d", response.StatusCode)
		}
		return fmt.Errorf("dial gateway websocket: %w", err)
	}
	defer connection.Close(websocket.StatusNormalClosure, "connector stopping")
	connection.SetReadLimit(maxMessageBytes)

	incoming := make(chan protocol.Envelope, 16384)
	readErrors := make(chan error, 1)
	go c.readLoop(ctx, connection, incoming, readErrors)

	if err := c.send(connection, protocol.MessageHello, 0, protocol.Hello{
		ConnectorVersion: c.config.ConnectorVersion,
		ProtocolVersion:  protocol.Version,
		Platform:         runtime.GOOS + "/" + runtime.GOARCH,
		Hostname:         hostname(),
		StartedAt:        c.startedAt,
	}); err != nil {
		return err
	}
	if err := c.send(
		connection,
		protocol.MessageInventorySnapshot,
		0,
		toInventoryWire(c.supervisor.Inventory()),
	); err != nil {
		return err
	}
	receipts, err := c.state.Receipts()
	if err != nil {
		return c.degradePersistence("load durable command receipts", err)
	}
	taskResults, err := c.state.AgentTaskResults()
	if err != nil {
		return c.degradePersistence("load durable AgentTask result outbox", err)
	}
	pending, err := c.outbox.Pending()
	if err != nil {
		return c.degradePersistence("load event outbox", err)
	}
	replay, err := newEventReplay(pending)
	if err != nil {
		return err
	}
	receiptIndex := 0
	resultIndex := 0

	heartbeatTicker := time.NewTicker(c.config.HeartbeatInterval)
	defer heartbeatTicker.Stop()
	inventoryTicker := time.NewTicker(c.config.InventoryInterval)
	defer inventoryTicker.Stop()
	replayReady := make(chan struct{})
	close(replayReady)

	processIncoming := func(envelope protocol.Envelope) error {
		if envelope.Type == protocol.MessageEventAck {
			ack, err := decodeEventAck(envelope.Payload)
			if err != nil {
				return err
			}
			if !replay.CanAcknowledge(ack.ThroughSequence) {
				return fmt.Errorf(
					"gateway acknowledged unsent event sequence %d (highest sent %d)",
					ack.ThroughSequence,
					replay.highestSent,
				)
			}
			if err := c.handleIncoming(ctx, connection, envelope); err != nil {
				return err
			}
			replay.AcknowledgeThrough(ack.ThroughSequence)
			return nil
		}
		return c.handleIncoming(ctx, connection, envelope)
	}

	for {
		for sent := 0; sent < replayBurst; {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case err := <-readErrors:
				return err
			case envelope := <-incoming:
				if err := processIncoming(envelope); err != nil {
					return err
				}
				continue
			default:
			}

			if resultIndex < len(taskResults) {
				result := taskResults[resultIndex]
				if err := c.send(
					connection,
					protocol.MessageAgentTaskResult,
					0,
					result,
				); err != nil {
					return err
				}
				resultIndex++
				sent++
				continue
			}
			if receiptIndex < len(receipts) {
				receipt := receipts[receiptIndex]
				if err := c.send(
					connection,
					protocol.MessageCommandAck,
					0,
					toCommandAckWire(protocol.Command{BindingEpoch: receipt.BindingEpoch}, receipt),
				); err != nil {
					return err
				}
				receiptIndex++
				sent++
				continue
			}
			event, available := replay.Next()
			if !available {
				break
			}
			if err := c.send(
				connection,
				protocol.MessageRuntimeEvent,
				event.Sequence,
				toRuntimeEventWire(event),
			); err != nil {
				return err
			}
			replay.MarkSent(event)
			sent++
		}

		var replayWake <-chan struct{}
		if resultIndex < len(taskResults) {
			replayWake = replayReady
		} else if receiptIndex < len(receipts) {
			replayWake = replayReady
		} else if _, available := replay.Next(); available {
			replayWake = replayReady
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-readErrors:
			return err
		case envelope := <-incoming:
			if err := processIncoming(envelope); err != nil {
				return err
			}
		case event := <-c.supervisor.Events():
			if err := c.persistEvent(&event); err != nil {
				return err
			}
			if err := replay.Add(event); err != nil {
				return err
			}
		case update := <-c.supervisor.Acks():
			sendErr := c.send(
				connection,
				protocol.MessageCommandAck,
				0,
				toCommandAckWire(update.Command, update.Ack),
			)
			if update.PersistenceError != nil {
				degradedErr := c.degradePersistence(
					"persist terminal command receipt",
					update.PersistenceError,
				)
				if sendErr != nil {
					return fmt.Errorf("%w; send receipt failure acknowledgement: %v", degradedErr, sendErr)
				}
				return degradedErr
			}
			if sendErr != nil {
				c.supervisor.RequeueAck(update)
				return sendErr
			}
		case result := <-c.supervisor.Results():
			if err := c.send(
				connection,
				protocol.MessageAgentTaskResult,
				0,
				result,
			); err != nil {
				return err
			}
		case <-replayWake:
		case <-heartbeatTicker.C:
			sessions, tasks := c.supervisor.Health()
			heartbeat := protocol.Heartbeat{
				ObservedAt:     time.Now().UTC(),
				UptimeSeconds:  int64(time.Since(c.startedAt).Seconds()),
				ActiveSessions: sessions,
				ActiveTasks:    tasks,
			}
			if err := c.send(connection, protocol.MessageHeartbeat, 0, heartbeat); err != nil {
				return err
			}
		case <-inventoryTicker.C:
			inventory := c.supervisor.Probe(ctx)
			if err := c.send(
				connection,
				protocol.MessageInventorySnapshot,
				0,
				toInventoryWire(inventory),
			); err != nil {
				return err
			}
		}
	}
}

func (c *Client) handleIncoming(
	ctx context.Context,
	connection *websocket.Conn,
	envelope protocol.Envelope,
) error {
	if envelope.ProtocolVersion != protocol.Version {
		return fmt.Errorf("server selected unsupported protocol version %q", envelope.ProtocolVersion)
	}
	if envelope.DeviceID != "" && envelope.DeviceID != c.config.Credentials.DeviceID {
		return errors.New("received message for a different device")
	}
	switch envelope.Type {
	case messageWelcome, messageAck:
		return nil
	case messageError:
		var payload struct {
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(envelope.Payload, &payload)
		if payload.Detail == "" {
			payload.Detail = "gateway rejected a connector message"
		}
		return errors.New(payload.Detail)
	case protocol.MessageCommand:
		command, err := protocol.DecodeCommand(envelope.Payload)
		if err != nil {
			return err
		}
		if degradedErr := c.persistenceDegraded(); degradedErr != nil {
			rejection := protocol.NewAck(
				command,
				"rejected",
				"connector_persistence_degraded",
				"connector local persistence is degraded; resolve the local storage error and restart",
				nil,
			)
			if err := c.send(
				connection,
				protocol.MessageCommandAck,
				0,
				toCommandAckWire(command, rejection),
			); err != nil {
				return fmt.Errorf("%w; send degraded command rejection: %v", degradedErr, err)
			}
			return degradedErr
		}
		result := c.supervisor.Handle(ctx, command)
		sendErr := c.send(
			connection,
			protocol.MessageCommandAck,
			0,
			toCommandAckWire(command, result.Ack),
		)
		if result.PersistenceError != nil {
			degradedErr := c.degradePersistence(
				"persist command receipt",
				result.PersistenceError,
			)
			if sendErr != nil {
				return fmt.Errorf("%w; send receipt failure acknowledgement: %v", degradedErr, sendErr)
			}
			return degradedErr
		}
		if sendErr != nil {
			return sendErr
		}
		if result.Inventory != nil {
			return c.send(
				connection,
				protocol.MessageInventorySnapshot,
				0,
				toInventoryWire(*result.Inventory),
			)
		}
		return nil
	case protocol.MessageEventAck:
		ack, err := decodeEventAck(envelope.Payload)
		if err != nil {
			return err
		}
		if err := c.outbox.AckThrough(ack.ThroughSequence); err != nil {
			return c.degradePersistence("acknowledge event outbox", err)
		}
		return nil
	case protocol.MessageAgentTaskResultAck:
		var ack struct {
			TaskID   string `json:"task_id"`
			ResultID string `json:"result_id"`
		}
		if err := json.Unmarshal(envelope.Payload, &ack); err != nil {
			return fmt.Errorf("decode AgentTask result acknowledgement: %w", err)
		}
		if strings.TrimSpace(ack.TaskID) == "" ||
			strings.TrimSpace(ack.ResultID) == "" {
			return errors.New(
				"AgentTask result acknowledgement requires task_id and result_id",
			)
		}
		if err := c.state.AckAgentTaskResult(ack.TaskID, ack.ResultID); err != nil {
			return c.degradePersistence("acknowledge AgentTask result outbox", err)
		}
		return nil
	default:
		return fmt.Errorf("unsupported server message type %q", envelope.Type)
	}
}

func decodeEventAck(payload json.RawMessage) (protocol.EventAck, error) {
	var ack protocol.EventAck
	if err := json.Unmarshal(payload, &ack); err != nil {
		return protocol.EventAck{}, fmt.Errorf("decode event ack: %w", err)
	}
	return ack, nil
}

func (c *Client) readLoop(
	ctx context.Context,
	connection *websocket.Conn,
	incoming chan<- protocol.Envelope,
	readErrors chan<- error,
) {
	for {
		var envelope protocol.Envelope
		if err := wsjson.Read(ctx, connection, &envelope); err != nil {
			select {
			case readErrors <- fmt.Errorf("read gateway message: %w", err):
			default:
			}
			return
		}
		select {
		case incoming <- envelope:
		case <-ctx.Done():
			return
		}
	}
}

func (c *Client) send(
	connection *websocket.Conn,
	messageType string,
	sequence uint64,
	payload any,
) error {
	envelope, err := protocol.NewEnvelope(messageType, c.config.Credentials.DeviceID, sequence, payload)
	if err != nil {
		return err
	}
	writeContext, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := wsjson.Write(writeContext, connection, envelope); err != nil {
		return fmt.Errorf("send %s: %w", messageType, err)
	}
	return nil
}

func (c *Client) persistEvent(event *protocol.RuntimeEvent) error {
	if err := c.recoverStagedEvent(); err != nil {
		return err
	}
	if event.Sequence != 0 {
		return errors.New("new runtime event already has a sequence")
	}
	staged, err := c.state.StageEvent(*event)
	if err != nil {
		return c.degradePersistence("stage event with durable sequence", err)
	}
	*event = staged
	if err := c.outbox.Append(staged); err != nil {
		return c.degradePersistence("append event outbox", err)
	}
	if err := c.state.ClearStagedEvent(staged.Sequence); err != nil {
		return c.degradePersistence("clear staged event", err)
	}
	return nil
}

func (c *Client) recoverStagedEvent() error {
	staged, err := c.state.StagedEvent()
	if err != nil {
		return c.degradePersistence("load staged event", err)
	}
	if staged == nil {
		return nil
	}
	if err := c.outbox.Append(*staged); err != nil {
		return c.degradePersistence("append staged event to outbox", err)
	}
	if err := c.state.ClearStagedEvent(staged.Sequence); err != nil {
		return c.degradePersistence("clear recovered staged event", err)
	}
	return nil
}

func (c *Client) degradePersistence(operation string, cause error) error {
	if cause == nil {
		return nil
	}
	diagnostic := fmt.Errorf("%s: %w", operation, cause)
	firstFailure := false
	c.persistenceMu.Lock()
	if c.persistenceFault == nil {
		c.persistenceFault = diagnostic
		firstFailure = true
	}
	latched := c.persistenceFault
	c.persistenceMu.Unlock()
	if firstFailure && c.logger != nil {
		c.logger.Printf(
			"connector entered fail-closed persistence-degraded state; no new commands will execute: %v",
			latched,
		)
	}
	return fmt.Errorf("%w: %w", ErrPersistenceDegraded, latched)
}

func (c *Client) persistenceDegraded() error {
	c.persistenceMu.RLock()
	defer c.persistenceMu.RUnlock()
	if c.persistenceFault == nil {
		return nil
	}
	return fmt.Errorf("%w: %w", ErrPersistenceDegraded, c.persistenceFault)
}

func validateGatewayURL(raw string) (*url.URL, error) {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return nil, fmt.Errorf("parse gateway URL: %w", err)
	}
	if parsed.Host == "" || (parsed.Scheme != "wss" && parsed.Scheme != "ws") {
		return nil, errors.New("gateway URL must use ws or wss")
	}
	if parsed.Scheme == "ws" && !isLoopbackHost(parsed.Hostname()) {
		return nil, errors.New("unencrypted websocket is allowed only for localhost")
	}
	return parsed, nil
}

func gatewayEndpoint(raw, deviceID string) (string, error) {
	parsed, err := validateGatewayURL(raw)
	if err != nil {
		return "", err
	}
	query := parsed.Query()
	query.Set("device_id", deviceID)
	parsed.RawQuery = query.Encode()
	return parsed.String(), nil
}

func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	address := net.ParseIP(host)
	return address != nil && address.IsLoopback()
}

func jitter(base time.Duration) time.Duration {
	if base <= 0 {
		return 0
	}
	factor := 0.8 + rand.Float64()*0.4
	return time.Duration(float64(base) * factor)
}

func toInventoryWire(inventory protocol.InventorySnapshot) inventoryWire {
	wire := inventoryWire{
		ObservedAt: inventory.ObservedAt,
		Host:       inventory.Host,
		Runtimes:   make([]runtimeWire, 0, len(inventory.Runtimes)),
	}
	for _, runtimeInfo := range inventory.Runtimes {
		kind := runtimeInfo.Kind
		if kind == "claude_code" {
			kind = "claude-code"
		}
		wire.Runtimes = append(wire.Runtimes, runtimeWire{
			RuntimeID:            runtimeInfo.ID,
			Kind:                 kind,
			DisplayName:          runtimeInfo.DisplayName,
			ExecutablePath:       runtimeInfo.ExecutablePath,
			Version:              runtimeInfo.Version,
			Available:            runtimeInfo.Available,
			Capabilities:         append([]string(nil), runtimeInfo.Capabilities...),
			AuthModes:            append([]string(nil), runtimeInfo.AuthModes...),
			TaskEnabled:          runtimeInfo.TaskEnabled,
			AuthenticationStatus: runtimeInfo.AuthenticationStatus,
			ArenaCompatible:      runtimeInfo.ArenaCompatible,
			ArenaIsolation:       runtimeInfo.ArenaIsolation,
			LocalExecutionReady:  runtimeInfo.LocalExecutionReady,
			ReadinessIssues:      append([]string(nil), runtimeInfo.ReadinessIssues...),
			DetectedAt:           runtimeInfo.DetectedAt,
		})
	}
	return wire
}

func toCommandAckWire(command protocol.Command, ack protocol.CommandAck) commandAckWire {
	status := ack.Status
	switch status {
	case "completed":
		status = "succeeded"
	case "accepted":
		status = "accepted"
	case "rejected":
		status = "rejected"
	default:
		status = "failed"
	}
	wire := commandAckWire{
		CommandID:    ack.CommandID,
		BindingEpoch: ack.BindingEpoch,
		Status:       status,
	}
	if wire.BindingEpoch == 0 {
		wire.BindingEpoch = command.BindingEpoch
	}
	if len(ack.Result) != 0 {
		_ = json.Unmarshal(ack.Result, &wire.Result)
	}
	if status == "rejected" || status == "failed" {
		wire.Error = map[string]any{"code": ack.Code, "message": ack.Message}
	}
	return wire
}

func toRuntimeEventWire(event protocol.RuntimeEvent) runtimeEventWire {
	level := "info"
	if strings.Contains(event.Type, "error") {
		level = "error"
	}
	data := make(map[string]any, len(event.Data)+1)
	for key, value := range event.Data {
		data[key] = value
	}
	if event.RuntimeID != "" {
		data["runtime_id"] = event.RuntimeID
	}
	return runtimeEventWire{
		EventID:    event.EventID,
		BindingID:  event.BindingID,
		SessionID:  event.SessionID,
		TaskID:     event.TaskID,
		Sequence:   event.Sequence,
		EventType:  event.Type,
		OccurredAt: event.ObservedAt,
		Level:      level,
		Data:       data,
	}
}

func hostname() string {
	value, err := os.Hostname()
	if err != nil {
		return ""
	}
	return value
}

package protocol

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

const (
	Version = "1.0"

	MessageHello              = "hello"
	MessageInventorySnapshot  = "inventory.snapshot"
	MessageHeartbeat          = "heartbeat"
	MessageCommand            = "command"
	MessageCommandAck         = "command.ack"
	MessageRuntimeEvent       = "runtime.event"
	MessageEventAck           = "event.ack"
	MessageAgentTaskResult    = "agent_task.result"
	MessageAgentTaskResultAck = "agent_task.result.ack"
	MessageTaskAvailable      = "task.available"
	MessageTaskAvailableAck   = "task.available.ack"

	CommandRuntimeProbe  = "runtime.probe"
	CommandSessionStart  = "session.start"
	CommandTaskDispatch  = "task.dispatch"
	CommandTaskCancel    = "task.cancel"
	CommandSessionStop   = "session.stop"
	CommandSessionResume = "session.resume"
)

var allowedCommands = map[string]struct{}{
	CommandRuntimeProbe:  {},
	CommandSessionStart:  {},
	CommandTaskDispatch:  {},
	CommandTaskCancel:    {},
	CommandSessionStop:   {},
	CommandSessionResume: {},
}

type Envelope struct {
	ProtocolVersion string          `json:"protocol_version"`
	Type            string          `json:"type"`
	MessageID       string          `json:"message_id"`
	DeviceID        string          `json:"device_id"`
	SentAt          time.Time       `json:"sent_at"`
	Sequence        uint64          `json:"sequence,omitempty"`
	Payload         json.RawMessage `json:"payload"`
}

func NewEnvelope(messageType, deviceID string, sequence uint64, payload any) (Envelope, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return Envelope{}, fmt.Errorf("marshal %s payload: %w", messageType, err)
	}
	return Envelope{
		ProtocolVersion: Version,
		Type:            messageType,
		MessageID:       NewID("msg"),
		DeviceID:        deviceID,
		SentAt:          time.Now().UTC(),
		Sequence:        sequence,
		Payload:         raw,
	}, nil
}

func (e Envelope) Validate() error {
	if e.ProtocolVersion != Version {
		return fmt.Errorf("unsupported protocol version %q", e.ProtocolVersion)
	}
	if e.Type == "" || e.MessageID == "" || e.DeviceID == "" {
		return errors.New("envelope type, message_id, and device_id are required")
	}
	if len(e.Payload) == 0 {
		return errors.New("envelope payload is required")
	}
	return nil
}

type HostInfo struct {
	Hostname         string `json:"hostname"`
	OS               string `json:"os"`
	Architecture     string `json:"architecture"`
	ConnectorVersion string `json:"connector_version"`
}

type Runtime struct {
	ID                   string    `json:"runtime_id"`
	Kind                 string    `json:"kind"`
	DisplayName          string    `json:"display_name"`
	ExecutablePath       string    `json:"executable_path"`
	Version              string    `json:"version,omitempty"`
	Status               string    `json:"status"`
	StatusDetail         string    `json:"status_detail,omitempty"`
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

type InventorySnapshot struct {
	ObservedAt time.Time `json:"observed_at"`
	Host       HostInfo  `json:"host"`
	Runtimes   []Runtime `json:"runtimes"`
}

type Hello struct {
	ConnectorVersion string    `json:"connector_version"`
	ProtocolVersion  string    `json:"protocol_version"`
	Platform         string    `json:"platform"`
	Hostname         string    `json:"hostname"`
	StartedAt        time.Time `json:"started_at"`
}

type Heartbeat struct {
	ObservedAt     time.Time `json:"observed_at"`
	UptimeSeconds  int64     `json:"uptime_seconds"`
	ActiveSessions int       `json:"active_sessions"`
	ActiveTasks    int       `json:"active_tasks"`
}

type Command struct {
	CommandID      string          `json:"command_id"`
	BindingID      string          `json:"binding_id,omitempty"`
	AgentID        string          `json:"agent_id,omitempty"`
	Kind           string          `json:"kind"`
	Action         string          `json:"action,omitempty"`
	IdempotencyKey string          `json:"idempotency_key"`
	RuntimeID      string          `json:"runtime_id,omitempty"`
	SessionID      string          `json:"session_id,omitempty"`
	BindingEpoch   uint64          `json:"binding_epoch,omitempty"`
	ExpiresAt      time.Time       `json:"expires_at"`
	Payload        json.RawMessage `json:"payload"`
}

func (c Command) Validate(now time.Time) error {
	if c.CommandID == "" {
		return errors.New("command_id is required")
	}
	if c.IdempotencyKey == "" {
		return errors.New("idempotency_key is required")
	}
	if c.BindingID == "" {
		return errors.New("binding_id is required")
	}
	kind := c.CommandKind()
	if _, ok := allowedCommands[kind]; !ok {
		return fmt.Errorf("unsupported command kind %q", kind)
	}
	if c.ExpiresAt.IsZero() {
		return errors.New("expires_at is required")
	}
	if !now.Before(c.ExpiresAt) {
		return errors.New("command has expired")
	}
	if kind != CommandRuntimeProbe {
		if c.AgentID == "" {
			return errors.New("agent_id is required")
		}
		if c.RuntimeID == "" {
			return errors.New("runtime_id is required")
		}
		if c.BindingEpoch == 0 {
			return errors.New("binding_epoch is required")
		}
	}
	if kind != CommandRuntimeProbe && kind != CommandSessionStart && c.SessionID == "" {
		return errors.New("session_id is required")
	}
	if len(c.Payload) == 0 {
		return errors.New("command payload is required")
	}
	return nil
}

func (c Command) CommandKind() string {
	if c.Kind != "" {
		return c.Kind
	}
	return c.Action
}

func DecodeCommand(raw json.RawMessage) (Command, error) {
	var command Command
	if err := json.Unmarshal(raw, &command); err != nil {
		return Command{}, fmt.Errorf("decode command: %w", err)
	}
	var metadata struct {
		RuntimeID string `json:"runtime_id"`
		SessionID string `json:"session_id"`
	}
	_ = json.Unmarshal(command.Payload, &metadata)
	if command.RuntimeID == "" {
		command.RuntimeID = metadata.RuntimeID
	}
	if command.SessionID == "" {
		command.SessionID = metadata.SessionID
	}
	return command, nil
}

type CommandAck struct {
	CommandID      string          `json:"command_id"`
	IdempotencyKey string          `json:"idempotency_key"`
	BindingEpoch   uint64          `json:"binding_epoch"`
	Status         string          `json:"status"`
	Code           string          `json:"code,omitempty"`
	Message        string          `json:"message,omitempty"`
	Result         json.RawMessage `json:"result,omitempty"`
	RecordedAt     time.Time       `json:"recorded_at"`
}

func NewAck(command Command, status, code, message string, result any) CommandAck {
	ack := CommandAck{
		CommandID:      command.CommandID,
		IdempotencyKey: command.IdempotencyKey,
		BindingEpoch:   command.BindingEpoch,
		Status:         status,
		Code:           code,
		Message:        message,
		RecordedAt:     time.Now().UTC(),
	}
	if result != nil {
		ack.Result, _ = json.Marshal(result)
	}
	return ack
}

type RuntimeEvent struct {
	EventID    string         `json:"event_id"`
	BindingID  string         `json:"binding_id,omitempty"`
	Sequence   uint64         `json:"sequence,omitempty"`
	RuntimeID  string         `json:"runtime_id"`
	SessionID  string         `json:"session_id"`
	TaskID     string         `json:"task_id,omitempty"`
	Type       string         `json:"type"`
	ObservedAt time.Time      `json:"observed_at"`
	Data       map[string]any `json:"data,omitempty"`
}

func NewRuntimeEvent(runtimeID, sessionID, taskID, eventType string, data map[string]any) RuntimeEvent {
	return RuntimeEvent{
		EventID:    NewID("evt"),
		RuntimeID:  runtimeID,
		SessionID:  sessionID,
		TaskID:     taskID,
		Type:       eventType,
		ObservedAt: time.Now().UTC(),
		Data:       data,
	}
}

type EventAck struct {
	ThroughSequence uint64 `json:"through_sequence"`
}

type AgentTaskResult struct {
	SchemaVersion string          `json:"schemaVersion"`
	ResultID      string          `json:"resultId"`
	TaskID        string          `json:"taskId"`
	Status        string          `json:"status"`
	Action        json.RawMessage `json:"action,omitempty"`
}

func (r AgentTaskResult) Validate() error {
	if r.SchemaVersion != "arena.agent-result.v1" {
		return fmt.Errorf("unsupported AgentTaskResult schema version %q", r.SchemaVersion)
	}
	if r.ResultID == "" || r.TaskID == "" {
		return errors.New("AgentTaskResult resultId and taskId are required")
	}
	switch r.Status {
	case "succeeded":
		if len(r.Action) == 0 {
			return errors.New("succeeded AgentTaskResult requires an action")
		}
	case "failed", "timed_out", "cancelled":
		if len(r.Action) != 0 {
			return fmt.Errorf("%s AgentTaskResult must not include an action", r.Status)
		}
	default:
		return fmt.Errorf("unsupported AgentTaskResult status %q", r.Status)
	}
	return nil
}

type AgentTaskResultEnvelope struct {
	BindingID    string          `json:"binding_id"`
	BindingEpoch uint64          `json:"binding_epoch"`
	Result       AgentTaskResult `json:"result"`
}

func (r AgentTaskResultEnvelope) Validate() error {
	if r.BindingID == "" || r.BindingEpoch == 0 {
		return errors.New("AgentTaskResult binding_id and binding_epoch are required")
	}
	return r.Result.Validate()
}

func NewAgentTaskResultID(bindingID, taskID, idempotencyKey string) string {
	digest := sha256.Sum256(
		[]byte(bindingID + "\x1f" + taskID + "\x1f" + idempotencyKey),
	)
	return "result-" + hex.EncodeToString(digest[:12])
}

func NewID(prefix string) string {
	var random [12]byte
	if _, err := rand.Read(random[:]); err != nil {
		return fmt.Sprintf("%s-%d", prefix, time.Now().UnixNano())
	}
	return prefix + "-" + hex.EncodeToString(random[:])
}

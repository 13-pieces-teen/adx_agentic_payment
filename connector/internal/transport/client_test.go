package transport

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/driver"
	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
	"github.com/adx-agentic-payment/adx/connector/internal/store"
	"github.com/adx-agentic-payment/adx/connector/internal/supervisor"
	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

type transportScanner struct {
	inventory protocol.InventorySnapshot
}

func (s transportScanner) Scan(context.Context) protocol.InventorySnapshot {
	return s.inventory
}

type faultOutbox struct {
	delegate *store.FileOutbox
	fail     bool
}

type faultReceiptStore struct {
	delegate   supervisor.ReceiptStore
	saveCalls  int
	failSaveAt int
	failLookup bool
}

func (s *faultReceiptStore) LookupReceipt(key string) (protocol.CommandAck, bool, error) {
	if s.failLookup {
		return protocol.CommandAck{}, false, errors.New("injected receipt lookup failure")
	}
	return s.delegate.LookupReceipt(key)
}

func (s *faultReceiptStore) SaveReceipt(key string, ack protocol.CommandAck) error {
	s.saveCalls++
	if s.failSaveAt != 0 && s.saveCalls == s.failSaveAt {
		return errors.New("injected receipt save failure")
	}
	return s.delegate.SaveReceipt(key, ack)
}

func (s *faultReceiptStore) SaveAgentTaskResult(result protocol.AgentTaskResultEnvelope) error {
	return s.delegate.SaveAgentTaskResult(result)
}

func (o *faultOutbox) Append(event protocol.RuntimeEvent) error {
	if o.fail {
		return errors.New("injected append failure")
	}
	return o.delegate.Append(event)
}

func (o *faultOutbox) Pending() ([]protocol.RuntimeEvent, error) {
	return o.delegate.Pending()
}

func (o *faultOutbox) AckThrough(sequence uint64) error {
	return o.delegate.AckThrough(sequence)
}

func TestHandleIncomingAcceptsGatewayWelcomeAndGenericAck(t *testing.T) {
	client := &Client{config: Config{Credentials: store.Credentials{DeviceID: "device-1"}}}
	for _, messageType := range []string{messageWelcome, messageAck} {
		err := client.handleIncoming(context.Background(), nil, protocol.Envelope{
			ProtocolVersion: protocol.Version,
			Type:            messageType,
			DeviceID:        "device-1",
			Payload:         json.RawMessage(`{}`),
		})
		if err != nil {
			t.Fatalf("%s should be accepted: %v", messageType, err)
		}
	}
}

func TestHandleIncomingAcknowledgesDurableAgentTaskResult(t *testing.T) {
	fileStore := store.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	result := protocol.AgentTaskResultEnvelope{
		BindingID:    "binding-1",
		BindingEpoch: 2,
		Result: protocol.AgentTaskResult{
			SchemaVersion: "arena.agent-result.v1",
			ResultID:      "result-1",
			TaskID:        "task-1",
			Status:        "succeeded",
			Action:        json.RawMessage(`{"action":"pass"}`),
		},
	}
	if err := fileStore.SaveAgentTaskResult(result); err != nil {
		t.Fatal(err)
	}
	client := &Client{
		config: Config{Credentials: store.Credentials{DeviceID: "device-1"}},
		state:  fileStore,
	}
	payload, _ := json.Marshal(
		map[string]any{"task_id": "task-1", "result_id": "result-1"},
	)
	err := client.handleIncoming(context.Background(), nil, protocol.Envelope{
		ProtocolVersion: protocol.Version,
		Type:            protocol.MessageAgentTaskResultAck,
		DeviceID:        "device-1",
		Payload:         payload,
	})
	if err != nil {
		t.Fatal(err)
	}
	pending, err := fileStore.AgentTaskResults()
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 0 {
		t.Fatalf("acknowledged AgentTask result remains durable: %#v", pending)
	}
}

func TestConnectionReplaysDurableAgentTaskResultAndClearsItAfterAck(t *testing.T) {
	serverErrors := make(chan error, 1)
	received := make(chan protocol.AgentTaskResultEnvelope, 1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(response, request, nil)
		if err != nil {
			serverErrors <- err
			return
		}
		defer connection.Close(websocket.StatusNormalClosure, "test complete")
		for {
			var envelope protocol.Envelope
			if err := wsjson.Read(request.Context(), connection, &envelope); err != nil {
				serverErrors <- err
				return
			}
			if envelope.Type != protocol.MessageAgentTaskResult {
				continue
			}
			var result protocol.AgentTaskResultEnvelope
			if err := json.Unmarshal(envelope.Payload, &result); err != nil {
				serverErrors <- err
				return
			}
			received <- result
			ack, err := protocol.NewEnvelope(
				protocol.MessageAgentTaskResultAck,
				"device-1",
				0,
				map[string]any{
					"task_id":   result.Result.TaskID,
					"result_id": result.Result.ResultID,
				},
			)
			if err != nil {
				serverErrors <- err
				return
			}
			if err := wsjson.Write(request.Context(), connection, ack); err != nil {
				serverErrors <- err
				return
			}
			stop, err := protocol.NewEnvelope(
				messageError,
				"device-1",
				0,
				map[string]any{"detail": "test complete"},
			)
			if err != nil {
				serverErrors <- err
				return
			}
			if err := wsjson.Write(request.Context(), connection, stop); err != nil {
				serverErrors <- err
			}
			return
		}
	}))
	defer server.Close()

	statePath := filepath.Join(t.TempDir(), "state.json")
	fileStore := store.NewFileStore(statePath)
	expected := protocol.AgentTaskResultEnvelope{
		BindingID:    "binding-1",
		BindingEpoch: 4,
		Result: protocol.AgentTaskResult{
			SchemaVersion: "arena.agent-result.v1",
			ResultID:      "result-1",
			TaskID:        "task-1",
			Status:        "succeeded",
			Action:        json.RawMessage(`{"action":"sell","good":"energy"}`),
		},
	}
	if err := fileStore.SaveAgentTaskResult(expected); err != nil {
		t.Fatal(err)
	}
	processSupervisor := newTransportSupervisor(t, fileStore)
	defer processSupervisor.Shutdown()
	client, err := NewClient(
		Config{
			Credentials: store.Credentials{
				DeviceID:   "device-1",
				Token:      "device-token",
				GatewayURL: "ws" + strings.TrimPrefix(server.URL, "http"),
			},
			HeartbeatInterval: time.Hour,
			InventoryInterval: time.Hour,
		},
		fileStore,
		store.NewFileOutbox(statePath),
		processSupervisor,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	connectionResult := make(chan error, 1)
	go func() {
		connectionResult <- client.runConnection(ctx)
	}()

	select {
	case result := <-received:
		if result.Result.ResultID != expected.Result.ResultID ||
			result.BindingEpoch != expected.BindingEpoch {
			t.Fatalf("unexpected replayed AgentTask result: %#v", result)
		}
	case err := <-serverErrors:
		t.Fatal(err)
	case <-ctx.Done():
		t.Fatal("timed out waiting for durable AgentTask result replay")
	}
	select {
	case <-connectionResult:
	case err := <-serverErrors:
		t.Fatal(err)
	case <-ctx.Done():
		t.Fatal("connector did not process the AgentTask result acknowledgement")
	}
	pending, err := fileStore.AgentTaskResults()
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 0 {
		t.Fatalf("acknowledged AgentTask result remains durable: %#v", pending)
	}
}

func TestGatewayEndpointUsesQueryForDeviceIDNotToken(t *testing.T) {
	endpoint, err := gatewayEndpoint("wss://arena.example/api/connectors/ws?region=cn", "device-1")
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(endpoint)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Query().Get("device_id") != "device-1" {
		t.Fatalf("device_id missing from endpoint: %s", endpoint)
	}
	if parsed.Query().Get("token") != "" {
		t.Fatalf("token must never be placed in the URL: %s", endpoint)
	}
}

func TestWireAdaptersMatchGatewayContract(t *testing.T) {
	inventory := protocol.InventorySnapshot{
		ObservedAt: time.Now().UTC(),
		Runtimes: []protocol.Runtime{{
			ID:                   "runtime-1",
			Kind:                 "claude_code",
			DisplayName:          "Claude Code",
			Available:            true,
			Capabilities:         []string{"session.start"},
			TaskEnabled:          true,
			AuthenticationStatus: "configured",
			ArenaCompatible:      true,
			ArenaIsolation:       "no_tools_safe_mode_schema",
			LocalExecutionReady:  true,
		}},
	}
	inventoryPayload := toInventoryWire(inventory)
	if inventoryPayload.Runtimes[0].Kind != "claude-code" {
		t.Fatalf("unexpected wire kind: %s", inventoryPayload.Runtimes[0].Kind)
	}
	if !inventoryPayload.Runtimes[0].LocalExecutionReady ||
		inventoryPayload.Runtimes[0].AuthenticationStatus != "configured" ||
		!inventoryPayload.Runtimes[0].ArenaCompatible ||
		inventoryPayload.Runtimes[0].ArenaIsolation != "no_tools_safe_mode_schema" {
		t.Fatalf("runtime readiness was dropped from inventory wire: %#v", inventoryPayload.Runtimes[0])
	}

	rawResult, _ := json.Marshal(map[string]any{"session_id": "session-1"})
	ack := toCommandAckWire(
		protocol.Command{BindingEpoch: 4},
		protocol.CommandAck{CommandID: "cmd-1", Status: "completed", Result: rawResult},
	)
	if ack.Status != "succeeded" || ack.BindingEpoch != 4 || ack.Result["session_id"] != "session-1" {
		t.Fatalf("unexpected command ack: %#v", ack)
	}

	event := protocol.NewRuntimeEvent("runtime-1", "session-1", "task-1", "runtime.stdout", nil)
	event.BindingID = "binding-1"
	event.Sequence = 7
	eventPayload := toRuntimeEventWire(event)
	if eventPayload.BindingID != "binding-1" || eventPayload.EventType != "runtime.stdout" || eventPayload.Sequence != 7 {
		t.Fatalf("unexpected runtime event: %#v", eventPayload)
	}
}

func TestValidateGatewayURLRejectsRemotePlaintextWebsocket(t *testing.T) {
	if _, err := validateGatewayURL("ws://arena.example/api/connectors/ws"); err == nil {
		t.Fatal("remote plaintext websocket should be rejected")
	}
	if _, err := validateGatewayURL("ws://localhost:8000/api/connectors/ws"); err != nil {
		t.Fatalf("localhost websocket should be allowed for development: %v", err)
	}
}

func TestRunStopsWhenGatewayReplacesConnection(t *testing.T) {
	var connectionCount atomic.Int32
	serverErrors := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		connectionCount.Add(1)
		connection, err := websocket.Accept(response, request, nil)
		if err != nil {
			serverErrors <- err
			return
		}
		defer connection.Close(websocket.StatusNormalClosure, "test complete")
		for received := 0; received < 2; received++ {
			var envelope protocol.Envelope
			if err := wsjson.Read(request.Context(), connection, &envelope); err != nil {
				serverErrors <- err
				return
			}
		}
		if err := connection.Close(websocket.StatusCode(4409), "replaced by a newer connection"); err != nil {
			serverErrors <- err
		}
	}))
	defer server.Close()

	statePath := filepath.Join(t.TempDir(), "state.json")
	fileStore := store.NewFileStore(statePath)
	processSupervisor := newTransportSupervisor(t, fileStore)
	defer processSupervisor.Shutdown()
	client, err := NewClient(
		Config{
			Credentials: store.Credentials{
				DeviceID:   "device-1",
				Token:      "device-token",
				GatewayURL: "ws" + strings.TrimPrefix(server.URL, "http"),
			},
			HeartbeatInterval: time.Hour,
			InventoryInterval: time.Hour,
			ReconnectMin:      10 * time.Millisecond,
			ReconnectMax:      20 * time.Millisecond,
		},
		fileStore,
		store.NewFileOutbox(statePath),
		processSupervisor,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runResult := make(chan error, 1)
	go func() {
		runResult <- client.Run(ctx)
	}()

	select {
	case runErr := <-runResult:
		if !errors.Is(runErr, ErrConnectionReplaced) {
			t.Fatalf("Run() error = %v, want ErrConnectionReplaced", runErr)
		}
		if connectionCount.Load() != 1 {
			t.Fatalf("replacement close triggered %d connection attempts, want 1", connectionCount.Load())
		}
	case serverErr := <-serverErrors:
		t.Fatal(serverErr)
	case <-ctx.Done():
		t.Fatal("Run() reconnected instead of stopping after 4409")
	}
}

func TestRunStopsWhenDeviceIsRevoked(t *testing.T) {
	var connectionCount atomic.Int32
	serverErrors := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		connectionCount.Add(1)
		connection, err := websocket.Accept(response, request, nil)
		if err != nil {
			serverErrors <- err
			return
		}
		defer connection.Close(websocket.StatusNormalClosure, "test complete")
		for received := 0; received < 2; received++ {
			var envelope protocol.Envelope
			if err := wsjson.Read(request.Context(), connection, &envelope); err != nil {
				serverErrors <- err
				return
			}
		}
		if err := connection.Close(websocket.StatusCode(4403), "device revoked"); err != nil {
			serverErrors <- err
		}
	}))
	defer server.Close()

	statePath := filepath.Join(t.TempDir(), "state.json")
	fileStore := store.NewFileStore(statePath)
	processSupervisor := newTransportSupervisor(t, fileStore)
	defer processSupervisor.Shutdown()
	client, err := NewClient(
		Config{
			Credentials: store.Credentials{
				DeviceID:   "device-1",
				Token:      "device-token",
				GatewayURL: "ws" + strings.TrimPrefix(server.URL, "http"),
			},
			HeartbeatInterval: time.Hour,
			InventoryInterval: time.Hour,
			ReconnectMin:      10 * time.Millisecond,
			ReconnectMax:      20 * time.Millisecond,
		},
		fileStore,
		store.NewFileOutbox(statePath),
		processSupervisor,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runResult := make(chan error, 1)
	go func() {
		runResult <- client.Run(ctx)
	}()

	select {
	case runErr := <-runResult:
		if !errors.Is(runErr, ErrDeviceRevoked) {
			t.Fatalf("Run() error = %v, want ErrDeviceRevoked", runErr)
		}
		if connectionCount.Load() != 1 {
			t.Fatalf("revocation triggered %d connection attempts, want 1", connectionCount.Load())
		}
	case serverErr := <-serverErrors:
		t.Fatal(serverErr)
	case <-ctx.Done():
		t.Fatal("Run() reconnected instead of stopping after 4403")
	}
}

func TestRecoverStagedEventAfterAppendFailurePreservesSequence(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "state.json")
	fileStore := store.NewFileStore(statePath)
	first := protocol.NewRuntimeEvent(
		"runtime-1",
		"session-1",
		"task-1",
		"runtime.stdout",
		map[string]any{"text": "first"},
	)
	staged, err := fileStore.StageEvent(first)
	if err != nil {
		t.Fatal(err)
	}
	if staged.Sequence != 1 {
		t.Fatalf("unexpected staged sequence: %d", staged.Sequence)
	}

	outbox := &faultOutbox{
		delegate: store.NewFileOutbox(statePath),
		fail:     true,
	}
	client := &Client{state: fileStore, outbox: outbox}
	if err := client.recoverStagedEvent(); err == nil {
		t.Fatal("injected append failure should leave the event staged")
	}

	restartedStore := store.NewFileStore(statePath)
	stillStaged, err := restartedStore.StagedEvent()
	if err != nil {
		t.Fatal(err)
	}
	if stillStaged == nil || stillStaged.Sequence != 1 || stillStaged.EventID != first.EventID {
		t.Fatalf("staged event did not survive restart: %#v", stillStaged)
	}

	outbox.fail = false
	client.state = restartedStore
	if err := client.recoverStagedEvent(); err != nil {
		t.Fatal(err)
	}
	pending, err := outbox.Pending()
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 1 || pending[0].Sequence != 1 || pending[0].EventID != first.EventID {
		t.Fatalf("unexpected recovered outbox: %#v", pending)
	}
	if stillStaged, err := restartedStore.StagedEvent(); err != nil || stillStaged != nil {
		t.Fatalf("recovered event should be cleared from state: %#v, %v", stillStaged, err)
	}

	second := protocol.NewRuntimeEvent("runtime-1", "session-1", "task-2", "runtime.stdout", nil)
	if err := client.persistEvent(&second); err != nil {
		t.Fatal(err)
	}
	if second.Sequence != 2 {
		t.Fatalf("next event must follow recovered event without a gap: %d", second.Sequence)
	}
}

func TestPersistenceFailureLatchesDegradedStateAndRejectsNewCommands(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "state.json")
	fileStore := store.NewFileStore(statePath)
	processSupervisor := newTransportSupervisor(t, fileStore)
	defer processSupervisor.Shutdown()
	outbox := &faultOutbox{
		delegate: store.NewFileOutbox(statePath),
		fail:     true,
	}
	var logs bytes.Buffer
	client, err := NewClient(
		Config{
			Credentials: store.Credentials{
				DeviceID:   "device-1",
				Token:      "device-token",
				GatewayURL: "ws://localhost/connectors/ws",
			},
		},
		fileStore,
		outbox,
		processSupervisor,
		log.New(&logs, "", 0),
	)
	if err != nil {
		t.Fatal(err)
	}

	event := protocol.NewRuntimeEvent("runtime-1", "session-1", "task-1", "runtime.stdout", nil)
	persistErr := client.persistEvent(&event)
	if !errors.Is(persistErr, ErrPersistenceDegraded) {
		t.Fatalf("event persistence error = %v, want ErrPersistenceDegraded", persistErr)
	}
	if !strings.Contains(persistErr.Error(), "injected append failure") {
		t.Fatalf("persistence diagnostic was lost: %v", persistErr)
	}
	if !strings.Contains(logs.String(), "fail-closed persistence-degraded") ||
		!strings.Contains(logs.String(), "injected append failure") {
		t.Fatalf("degraded diagnostic was not logged: %s", logs.String())
	}

	command := protocol.Command{
		CommandID:      "cmd-after-persistence-failure",
		BindingID:      "binding-1",
		Kind:           protocol.CommandRuntimeProbe,
		IdempotencyKey: "idem-after-persistence-failure",
		ExpiresAt:      time.Now().Add(time.Minute).UTC(),
		Payload:        json.RawMessage(`{}`),
	}
	commandPayload, err := json.Marshal(command)
	if err != nil {
		t.Fatal(err)
	}
	handlerResult := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		connection, acceptErr := websocket.Accept(response, request, nil)
		if acceptErr != nil {
			handlerResult <- acceptErr
			return
		}
		defer connection.Close(websocket.StatusNormalClosure, "test complete")
		handlerResult <- client.handleIncoming(request.Context(), connection, protocol.Envelope{
			ProtocolVersion: protocol.Version,
			Type:            protocol.MessageCommand,
			DeviceID:        "device-1",
			Payload:         commandPayload,
		})
	}))
	defer server.Close()

	connection, _, err := websocket.Dial(
		context.Background(),
		"ws"+strings.TrimPrefix(server.URL, "http"),
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close(websocket.StatusNormalClosure, "test complete")
	var envelope protocol.Envelope
	if err := wsjson.Read(context.Background(), connection, &envelope); err != nil {
		t.Fatal(err)
	}
	var rejection commandAckWire
	if err := json.Unmarshal(envelope.Payload, &rejection); err != nil {
		t.Fatal(err)
	}
	if rejection.Status != "rejected" ||
		rejection.Error["code"] != "connector_persistence_degraded" {
		t.Fatalf("new command was not explicitly rejected: %#v", rejection)
	}
	if _, found, err := fileStore.LookupReceipt("binding-1\x1fidem-after-persistence-failure"); err != nil || found {
		t.Fatalf("degraded connector executed or claimed a new command: found=%v err=%v", found, err)
	}
	select {
	case handlerErr := <-handlerResult:
		if !errors.Is(handlerErr, ErrPersistenceDegraded) {
			t.Fatalf("handler error = %v, want ErrPersistenceDegraded", handlerErr)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for degraded command handler")
	}
}

func TestTerminalReceiptFailureLatchesPersistenceDegraded(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "state.json")
	fileStore := store.NewFileStore(statePath)
	receipts := &faultReceiptStore{
		delegate:   fileStore,
		failSaveAt: 2, // durable claim succeeds; terminal result persistence fails
	}
	processSupervisor := newTransportSupervisor(t, receipts)
	defer processSupervisor.Shutdown()
	client, err := NewClient(
		Config{
			Credentials: store.Credentials{
				DeviceID:   "device-1",
				Token:      "device-token",
				GatewayURL: "ws://localhost/connectors/ws",
			},
		},
		fileStore,
		store.NewFileOutbox(statePath),
		processSupervisor,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	command := protocol.Command{
		CommandID:      "cmd-terminal-receipt-failure",
		BindingID:      "binding-1",
		Kind:           protocol.CommandRuntimeProbe,
		IdempotencyKey: "idem-terminal-receipt-failure",
		ExpiresAt:      time.Now().Add(time.Minute).UTC(),
		Payload:        json.RawMessage(`{}`),
	}
	commandPayload, err := json.Marshal(command)
	if err != nil {
		t.Fatal(err)
	}
	handlerResult := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		connection, acceptErr := websocket.Accept(response, request, nil)
		if acceptErr != nil {
			handlerResult <- acceptErr
			return
		}
		defer connection.Close(websocket.StatusNormalClosure, "test complete")
		handlerResult <- client.handleIncoming(request.Context(), connection, protocol.Envelope{
			ProtocolVersion: protocol.Version,
			Type:            protocol.MessageCommand,
			DeviceID:        "device-1",
			Payload:         commandPayload,
		})
	}))
	defer server.Close()

	connection, _, err := websocket.Dial(
		context.Background(),
		"ws"+strings.TrimPrefix(server.URL, "http"),
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close(websocket.StatusNormalClosure, "test complete")
	var envelope protocol.Envelope
	if err := wsjson.Read(context.Background(), connection, &envelope); err != nil {
		t.Fatal(err)
	}
	var rejection commandAckWire
	if err := json.Unmarshal(envelope.Payload, &rejection); err != nil {
		t.Fatal(err)
	}
	if rejection.Status != "rejected" || rejection.Error["code"] != "receipt_store_error" {
		t.Fatalf("terminal receipt failure was not reported: %#v", rejection)
	}
	select {
	case handlerErr := <-handlerResult:
		if !errors.Is(handlerErr, ErrPersistenceDegraded) {
			t.Fatalf("handler error = %v, want ErrPersistenceDegraded", handlerErr)
		}
		if !strings.Contains(handlerErr.Error(), "injected receipt save failure") {
			t.Fatalf("receipt diagnostic was lost: %v", handlerErr)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for receipt persistence failure")
	}
	if degradedErr := client.persistenceDegraded(); !errors.Is(degradedErr, ErrPersistenceDegraded) {
		t.Fatalf("receipt failure did not latch degraded state: %v", degradedErr)
	}
	claim, found, err := fileStore.LookupReceipt("binding-1\x1fidem-terminal-receipt-failure")
	if err != nil || !found || claim.Status != "accepted" {
		t.Fatalf("durable claim should remain recoverable after terminal save failure: %#v, found=%v err=%v", claim, found, err)
	}
}

func TestEventReplayUsesBoundedWindowAndRejectsGaps(t *testing.T) {
	events := make([]protocol.RuntimeEvent, 0, replayWindow*2)
	for sequence := uint64(1); sequence <= replayWindow*2; sequence++ {
		event := protocol.NewRuntimeEvent("runtime-1", "session-1", "", "runtime.stdout", nil)
		event.Sequence = sequence
		events = append(events, event)
	}
	replay, err := newEventReplay(events)
	if err != nil {
		t.Fatal(err)
	}
	for sent := 0; sent < replayWindow; sent++ {
		event, ok := replay.Next()
		if !ok {
			t.Fatalf("replay stopped before filling window at %d", sent)
		}
		replay.MarkSent(event)
	}
	if _, ok := replay.Next(); ok {
		t.Fatal("replay exceeded its unacknowledged event window")
	}
	replay.AcknowledgeThrough(replayWindow / 2)
	for sent := 0; sent < replayWindow/2; sent++ {
		event, ok := replay.Next()
		if !ok {
			t.Fatalf("acknowledgement did not advance replay window at %d", sent)
		}
		replay.MarkSent(event)
	}

	gapped := append([]protocol.RuntimeEvent(nil), events[:2]...)
	gapped[1].Sequence = 3
	if _, err := newEventReplay(gapped); err == nil {
		t.Fatal("sequence gaps must fail closed instead of being skipped")
	}
}

func TestConnectionProcessesCommandWhileLargeBacklogReplays(t *testing.T) {
	const backlogSize = replayWindow * 8

	commandAckAt := make(chan int, 1)
	serverErrors := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(response, request, nil)
		if err != nil {
			serverErrors <- err
			return
		}
		defer connection.Close(websocket.StatusNormalClosure, "test complete")

		eventCount := 0
		commandSent := false
		for {
			var envelope protocol.Envelope
			if err := wsjson.Read(request.Context(), connection, &envelope); err != nil {
				serverErrors <- err
				return
			}
			switch envelope.Type {
			case protocol.MessageHello:
				welcome, err := protocol.NewEnvelope(messageWelcome, "device-1", 0, map[string]any{})
				if err != nil {
					serverErrors <- err
					return
				}
				if err := wsjson.Write(request.Context(), connection, welcome); err != nil {
					serverErrors <- err
					return
				}
			case protocol.MessageRuntimeEvent:
				eventCount++
				if !commandSent {
					commandSent = true
					command := protocol.Command{
						CommandID:      "cmd-during-replay",
						BindingID:      "binding-1",
						Kind:           protocol.CommandRuntimeProbe,
						IdempotencyKey: "idem-during-replay",
						ExpiresAt:      time.Now().Add(time.Minute).UTC(),
						Payload:        json.RawMessage(`{}`),
					}
					commandEnvelope, err := protocol.NewEnvelope(
						protocol.MessageCommand,
						"device-1",
						0,
						command,
					)
					if err != nil {
						serverErrors <- err
						return
					}
					if err := wsjson.Write(request.Context(), connection, commandEnvelope); err != nil {
						serverErrors <- err
						return
					}
				}
				ack, err := protocol.NewEnvelope(
					protocol.MessageEventAck,
					"device-1",
					0,
					protocol.EventAck{ThroughSequence: envelope.Sequence},
				)
				if err != nil {
					serverErrors <- err
					return
				}
				if err := wsjson.Write(request.Context(), connection, ack); err != nil {
					serverErrors <- err
					return
				}
			case protocol.MessageCommandAck:
				var ack commandAckWire
				if err := json.Unmarshal(envelope.Payload, &ack); err != nil {
					serverErrors <- err
					return
				}
				if ack.CommandID == "cmd-during-replay" {
					commandAckAt <- eventCount
					<-request.Context().Done()
					return
				}
			}
		}
	}))
	defer server.Close()

	statePath := filepath.Join(t.TempDir(), "state.json")
	fileStore := store.NewFileStore(statePath)
	outbox := store.NewFileOutbox(statePath)
	for sequence := uint64(1); sequence <= backlogSize; sequence++ {
		event := protocol.NewRuntimeEvent("runtime-1", "session-1", "", "runtime.stdout", nil)
		event.BindingID = "binding-1"
		event.Sequence = sequence
		if err := outbox.Append(event); err != nil {
			t.Fatal(err)
		}
	}
	processSupervisor := newTransportSupervisor(t, fileStore)
	defer processSupervisor.Shutdown()
	gatewayURL := "ws" + strings.TrimPrefix(server.URL, "http")
	client, err := NewClient(
		Config{
			Credentials: store.Credentials{
				DeviceID:   "device-1",
				Token:      "device-token",
				GatewayURL: gatewayURL,
			},
			HeartbeatInterval: time.Hour,
			InventoryInterval: time.Hour,
		},
		fileStore,
		outbox,
		processSupervisor,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	connectionResult := make(chan error, 1)
	go func() {
		connectionResult <- client.runConnection(ctx)
	}()

	select {
	case count := <-commandAckAt:
		if count >= backlogSize {
			t.Fatalf("command was processed only after the entire backlog (%d events)", count)
		}
		if count > replayWindow+replayBurst {
			t.Fatalf("command processing exceeded bounded replay window: %d events", count)
		}
		cancel()
	case err := <-serverErrors:
		t.Fatal(err)
	case err := <-connectionResult:
		t.Fatalf("connection ended before command acknowledgement: %v", err)
	case <-ctx.Done():
		t.Fatal("timed out waiting for command acknowledgement during replay")
	}
}

func TestRunRecoversAndReplaysInterruptedReceipt(t *testing.T) {
	replayed := make(chan commandAckWire, 1)
	serverErrors := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(response, request, nil)
		if err != nil {
			serverErrors <- err
			return
		}
		defer connection.Close(websocket.StatusNormalClosure, "test complete")
		for {
			var envelope protocol.Envelope
			if err := wsjson.Read(request.Context(), connection, &envelope); err != nil {
				serverErrors <- err
				return
			}
			if envelope.Type != protocol.MessageCommandAck {
				continue
			}
			var ack commandAckWire
			if err := json.Unmarshal(envelope.Payload, &ack); err != nil {
				serverErrors <- err
				return
			}
			if ack.CommandID == "cmd-interrupted" {
				replayed <- ack
				<-request.Context().Done()
				return
			}
		}
	}))
	defer server.Close()

	statePath := filepath.Join(t.TempDir(), "state.json")
	fileStore := store.NewFileStore(statePath)
	if err := fileStore.SaveReceipt("binding-1\x1fidem-interrupted", protocol.CommandAck{
		CommandID:      "cmd-interrupted",
		IdempotencyKey: "idem-interrupted",
		BindingEpoch:   3,
		Status:         "accepted",
		RecordedAt:     time.Now().Add(-time.Minute).UTC(),
	}); err != nil {
		t.Fatal(err)
	}
	processSupervisor := newTransportSupervisor(t, fileStore)
	defer processSupervisor.Shutdown()
	client, err := NewClient(
		Config{
			Credentials: store.Credentials{
				DeviceID:   "device-1",
				Token:      "device-token",
				GatewayURL: "ws" + strings.TrimPrefix(server.URL, "http"),
			},
			HeartbeatInterval: time.Hour,
			InventoryInterval: time.Hour,
			ReconnectMin:      time.Hour,
			ReconnectMax:      time.Hour,
		},
		fileStore,
		store.NewFileOutbox(statePath),
		processSupervisor,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	runResult := make(chan error, 1)
	go func() {
		runResult <- client.Run(ctx)
	}()

	select {
	case ack := <-replayed:
		if ack.Status != "failed" {
			t.Fatalf("interrupted receipt was replayed as non-terminal: %#v", ack)
		}
		if ack.BindingEpoch != 3 {
			t.Fatalf("binding epoch was not preserved: %#v", ack)
		}
		if ack.Error["code"] != "connector_restarted" {
			t.Fatalf("interruption reason was not replayed: %#v", ack)
		}
		cancel()
	case err := <-serverErrors:
		t.Fatal(err)
	case err := <-runResult:
		t.Fatalf("connector stopped before replaying receipt: %v", err)
	case <-ctx.Done():
		t.Fatal("timed out waiting for interrupted receipt replay")
	}
}

func newTransportSupervisor(t *testing.T, receipts supervisor.ReceiptStore) *supervisor.Supervisor {
	t.Helper()
	inventory := protocol.InventorySnapshot{
		ObservedAt: time.Now().UTC(),
		Host: protocol.HostInfo{
			Hostname: "test-host",
		},
	}
	root := t.TempDir()
	processSupervisor, err := supervisor.New(
		transportScanner{inventory: inventory},
		receipts,
		driver.DefaultRegistry(),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	return processSupervisor
}

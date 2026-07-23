package supervisor

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/driver"
	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
)

type fakeScanner struct {
	inventory protocol.InventorySnapshot
}

func (f fakeScanner) Scan(context.Context) protocol.InventorySnapshot {
	return f.inventory
}

type memoryReceipts struct {
	values map[string]protocol.CommandAck
}

type failSaveReceipts struct {
	delegate  *memoryReceipts
	saveCalls int
	failAt    int
}

type helperDriver struct{}

func (helperDriver) Kind() string {
	return "test_runtime"
}

func (helperDriver) BuildTask(
	ctx context.Context,
	_ protocol.Runtime,
	_ driver.SessionSpec,
	_ driver.TaskSpec,
) (*exec.Cmd, error) {
	process := exec.CommandContext(ctx, os.Args[0], "-test.run=TestSupervisorHelperProcess", "--")
	process.Env = append(os.Environ(), "ADX_CONNECTOR_HELPER_PROCESS=1")
	return process, nil
}

func TestSupervisorHelperProcess(t *testing.T) {
	if os.Getenv("ADX_CONNECTOR_HELPER_PROCESS") != "1" {
		return
	}
	fmt.Println(`{"session_id":"runtime-session-1","type":"result"}`)
	os.Exit(0)
}

func (m *memoryReceipts) LookupReceipt(key string) (protocol.CommandAck, bool, error) {
	value, ok := m.values[key]
	return value, ok, nil
}

func (m *memoryReceipts) SaveReceipt(key string, value protocol.CommandAck) error {
	m.values[key] = value
	return nil
}

func (f *failSaveReceipts) LookupReceipt(key string) (protocol.CommandAck, bool, error) {
	return f.delegate.LookupReceipt(key)
}

func (f *failSaveReceipts) SaveReceipt(key string, value protocol.CommandAck) error {
	f.saveCalls++
	if f.saveCalls == f.failAt {
		return fmt.Errorf("injected receipt save failure")
	}
	return f.delegate.SaveReceipt(key, value)
}

func command(kind string, payload any) protocol.Command {
	raw, _ := json.Marshal(payload)
	return protocol.Command{
		CommandID:      protocol.NewID("cmd"),
		BindingID:      "binding-1",
		AgentID:        "agent-1",
		Kind:           kind,
		IdempotencyKey: protocol.NewID("idem"),
		RuntimeID:      "runtime-1",
		SessionID:      "session-1",
		BindingEpoch:   1,
		ExpiresAt:      time.Now().Add(time.Minute),
		Payload:        raw,
	}
}

func newTestSupervisor(t *testing.T) *Supervisor {
	t.Helper()
	root := t.TempDir()
	inventory := protocol.InventorySnapshot{
		ObservedAt: time.Now().UTC(),
		Runtimes: []protocol.Runtime{{
			ID:             "runtime-1",
			Kind:           "claude_code",
			ExecutablePath: filepath.Join(root, "claude"),
			Status:         "ready",
			Available:      true,
		}},
	}
	s, err := New(
		fakeScanner{inventory: inventory},
		&memoryReceipts{values: make(map[string]protocol.CommandAck)},
		driver.DefaultRegistry(),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestSupervisorStartsStopsAndResumesLogicalSession(t *testing.T) {
	s := newTestSupervisor(t)
	root := s.allowedRoots[0]
	start := command(protocol.CommandSessionStart, map[string]any{"working_directory": root})
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}
	s.captureResumeToken("session-1", map[string]any{"session_id": "runtime-session-1"})

	stop := command(protocol.CommandSessionStop, map[string]any{})
	if result := s.Handle(context.Background(), stop); result.Ack.Status != "completed" {
		t.Fatalf("stop failed: %#v", result.Ack)
	}

	resume := command(protocol.CommandSessionResume, map[string]any{})
	if result := s.Handle(context.Background(), resume); result.Ack.Status != "completed" {
		t.Fatalf("resume failed: %#v", result.Ack)
	}
}

func TestSessionStartRejectsMissingWorkingDirectory(t *testing.T) {
	s := newTestSupervisor(t)
	start := command(protocol.CommandSessionStart, map[string]any{})
	result := s.Handle(context.Background(), start)
	if result.Ack.Status != "rejected" || result.Ack.Code != "invalid_payload" {
		t.Fatalf("start without an explicit working_directory should be rejected: %#v", result.Ack)
	}
}

func TestSupervisorRejectsWorkingDirectoryOutsideAllowlist(t *testing.T) {
	s := newTestSupervisor(t)
	start := command(protocol.CommandSessionStart, map[string]any{
		"working_directory": filepath.Dir(s.allowedRoots[0]),
	})
	result := s.Handle(context.Background(), start)
	if result.Ack.Status != "rejected" || result.Ack.Code != "working_directory_denied" {
		t.Fatalf("outside directory should be rejected: %#v", result.Ack)
	}
}

func TestDetectionOnlyRuntimeCannotDispatchTask(t *testing.T) {
	root := t.TempDir()
	inventory := protocol.InventorySnapshot{
		ObservedAt: time.Now().UTC(),
		Runtimes: []protocol.Runtime{{
			ID:             "runtime-1",
			Kind:           "detection_only",
			ExecutablePath: filepath.Join(root, "detected-runtime"),
			Status:         "ready",
			Available:      true,
		}},
	}
	s, err := New(
		fakeScanner{inventory: inventory},
		&memoryReceipts{values: make(map[string]protocol.CommandAck)},
		driver.NewRegistry(),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}

	start := command(protocol.CommandSessionStart, map[string]any{"working_directory": root})
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("logical session creation should not execute a process: %#v", result.Ack)
	}
	dispatch := command(protocol.CommandTaskDispatch, map[string]any{
		"request_id": "detection-only-task",
		"prompt":     "must not execute",
	})
	result := s.Handle(context.Background(), dispatch)
	if result.Ack.Status != "rejected" || result.Ack.Code != "driver_not_supported" {
		t.Fatalf("detection-only runtime must reject task dispatch: %#v", result.Ack)
	}
}

func TestSupervisorReturnsPersistedIdempotentReceipt(t *testing.T) {
	s := newTestSupervisor(t)
	start := command(protocol.CommandSessionStart, map[string]any{
		"working_directory": s.allowedRoots[0],
	})
	first := s.Handle(context.Background(), start)
	start.CommandID = "different-command-id"
	second := s.Handle(context.Background(), start)
	if first.Ack.CommandID != second.Ack.CommandID {
		t.Fatalf("expected original receipt, got %#v then %#v", first.Ack, second.Ack)
	}
}

func TestSupervisorRejectsStaleBindingEpoch(t *testing.T) {
	s := newTestSupervisor(t)
	start := command(protocol.CommandSessionStart, map[string]any{
		"working_directory": s.allowedRoots[0],
	})
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	for action, payload := range map[string]any{
		protocol.CommandSessionStart:  map[string]any{"working_directory": s.allowedRoots[0]},
		protocol.CommandTaskDispatch:  map[string]any{"request_id": "request-1", "prompt": "work"},
		protocol.CommandTaskCancel:    map[string]any{"request_id": "request-1"},
		protocol.CommandSessionStop:   map[string]any{},
		protocol.CommandSessionResume: map[string]any{},
	} {
		candidate := command(action, payload)
		candidate.BindingEpoch = 2
		result := s.Handle(context.Background(), candidate)
		if result.Ack.Code != "stale_binding" {
			t.Fatalf("%s with a stale binding should be rejected: %#v", action, result.Ack)
		}
	}
}

func TestSupervisorRejectsSessionOwnershipMismatchForEveryLifecycleCommand(t *testing.T) {
	s := newTestSupervisor(t)
	start := command(protocol.CommandSessionStart, map[string]any{
		"working_directory": s.allowedRoots[0],
	})
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	actions := map[string]any{
		protocol.CommandSessionStart:  map[string]any{"working_directory": s.allowedRoots[0]},
		protocol.CommandTaskDispatch:  map[string]any{"request_id": "request-1", "prompt": "work"},
		protocol.CommandTaskCancel:    map[string]any{"request_id": "request-1"},
		protocol.CommandSessionStop:   map[string]any{},
		protocol.CommandSessionResume: map[string]any{},
	}
	mutations := map[string]func(*protocol.Command){
		"binding": func(command *protocol.Command) { command.BindingID = "binding-other" },
		"agent":   func(command *protocol.Command) { command.AgentID = "agent-other" },
		"runtime": func(command *protocol.Command) { command.RuntimeID = "runtime-other" },
	}

	for action, payload := range actions {
		for identity, mutate := range mutations {
			candidate := command(action, payload)
			mutate(&candidate)
			result := s.Handle(context.Background(), candidate)
			if result.Ack.Code != "session_ownership_mismatch" {
				t.Fatalf(
					"%s with mismatched %s ownership should be rejected: %#v",
					action,
					identity,
					result.Ack,
				)
			}
		}
	}
}

func TestSupervisorAcceptsOnlyConnectorCapturedResumeToken(t *testing.T) {
	s := newTestSupervisor(t)
	cloudStart := command(protocol.CommandSessionStart, map[string]any{
		"working_directory": s.allowedRoots[0],
		"conversation_id":   "cloud-provider-session",
	})
	if result := s.Handle(context.Background(), cloudStart); result.Ack.Code != "cloud_resume_token_forbidden" {
		t.Fatalf("cloud conversation id must not create a resume token: %#v", result.Ack)
	}

	start := command(protocol.CommandSessionStart, map[string]any{
		"working_directory": s.allowedRoots[0],
	})
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}
	stop := command(protocol.CommandSessionStop, map[string]any{})
	if result := s.Handle(context.Background(), stop); result.Ack.Status != "completed" {
		t.Fatalf("stop failed: %#v", result.Ack)
	}

	for field, value := range map[string]string{
		"conversation_id": "cloud-provider-session",
		"resume_token":    "cloud-resume-token",
	} {
		resume := command(protocol.CommandSessionResume, map[string]any{field: value})
		if result := s.Handle(context.Background(), resume); result.Ack.Code != "cloud_resume_token_forbidden" {
			t.Fatalf("cloud %s must not overwrite a resume token: %#v", field, result.Ack)
		}
	}

	resumeWithoutToken := command(protocol.CommandSessionResume, map[string]any{})
	if result := s.Handle(context.Background(), resumeWithoutToken); result.Ack.Code != "resume_token_unavailable" {
		t.Fatalf("resume without a Connector-captured token must be rejected: %#v", result.Ack)
	}

	s.captureResumeToken("session-1", map[string]any{"thread_id": "connector-thread-1"})
	s.captureResumeToken("session-1", map[string]any{"thread_id": "later-thread-must-not-overwrite"})
	s.mu.RLock()
	captured := s.sessions["session-1"].ResumeToken
	capturedLocally := s.sessions["session-1"].ResumeTokenCaptured
	s.mu.RUnlock()
	if !capturedLocally || captured != "connector-thread-1" {
		t.Fatalf("expected the first Connector-captured token, got %q (captured=%t)", captured, capturedLocally)
	}

	resume := command(protocol.CommandSessionResume, map[string]any{})
	if result := s.Handle(context.Background(), resume); result.Ack.Status != "completed" {
		t.Fatalf("resume with a Connector-captured token failed: %#v", result.Ack)
	}
}

func TestTaskCancelReturnsTerminalCompletedReceipt(t *testing.T) {
	s := newTestSupervisor(t)
	start := command(protocol.CommandSessionStart, map[string]any{
		"working_directory": s.allowedRoots[0],
	})
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	cancelCalled := false
	running := &task{
		ID:      "task-1",
		command: &exec.Cmd{},
		cancel: func() {
			cancelCalled = true
		},
	}
	s.mu.Lock()
	s.sessions["session-1"].Tasks[running.ID] = running
	s.mu.Unlock()

	cancel := command(protocol.CommandTaskCancel, map[string]any{"task_id": running.ID})
	result := s.Handle(context.Background(), cancel)
	if result.Ack.Status != "completed" {
		t.Fatalf("task.cancel must reach a terminal state: %#v", result.Ack)
	}
	if !cancelCalled || !running.cancelled.Load() {
		t.Fatal("task.cancel completed without delivering cancellation to the managed task")
	}
}

func TestEnvironmentReferencesRequireLocalAllowlist(t *testing.T) {
	t.Setenv("ADX_TEST_AGENT_KEY", "local-secret")
	if _, err := buildEnvironment([]string{"ADX_TEST_AGENT_KEY"}, nil); err == nil {
		t.Fatal("cloud environment reference should be denied without local authorization")
	}
	environment, err := buildEnvironment(
		[]string{"ADX_TEST_AGENT_KEY"},
		normalizeEnvironmentAllowlist([]string{"ADX_TEST_AGENT_KEY"}),
	)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, item := range environment {
		if item == "ADX_TEST_AGENT_KEY=local-secret" {
			found = true
		}
	}
	if !found {
		t.Fatal("locally authorized reference was not included")
	}
}

func TestTaskDispatchEmitsTerminalCommandAck(t *testing.T) {
	root := t.TempDir()
	inventory := protocol.InventorySnapshot{
		ObservedAt: time.Now().UTC(),
		Runtimes: []protocol.Runtime{{
			ID:             "runtime-1",
			Kind:           "test_runtime",
			ExecutablePath: os.Args[0],
			Status:         "ready",
			Available:      true,
		}},
	}
	receipts := &memoryReceipts{values: make(map[string]protocol.CommandAck)}
	s, err := New(
		fakeScanner{inventory: inventory},
		receipts,
		driver.NewRegistry(helperDriver{}),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	start := command(protocol.CommandSessionStart, map[string]any{"working_directory": root})
	start.BindingID = "binding-1"
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	dispatch := command(protocol.CommandTaskDispatch, map[string]any{
		"request_id": "request-1",
		"prompt":     "run",
	})
	dispatch.BindingID = "binding-1"
	result := s.Handle(context.Background(), dispatch)
	if result.Ack.Status != "accepted" {
		t.Fatalf("dispatch should be accepted first: %#v", result.Ack)
	}

	select {
	case update := <-s.Acks():
		if update.Command.CommandID != dispatch.CommandID || update.Ack.Status != "completed" {
			t.Fatalf("unexpected terminal update: %#v", update)
		}
		if receipt := receipts.values[receiptKey(dispatch)]; receipt.Status != "completed" {
			t.Fatalf("terminal receipt was not persisted: %#v", receipt)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for terminal command ack")
	}
}

func TestTaskTerminalReceiptFailureIsPropagatedToTransport(t *testing.T) {
	root := t.TempDir()
	inventory := protocol.InventorySnapshot{
		ObservedAt: time.Now().UTC(),
		Runtimes: []protocol.Runtime{{
			ID:             "runtime-1",
			Kind:           "test_runtime",
			ExecutablePath: os.Args[0],
			Status:         "ready",
			Available:      true,
		}},
	}
	durable := &memoryReceipts{values: make(map[string]protocol.CommandAck)}
	receipts := &failSaveReceipts{
		delegate: durable,
		failAt:   4, // start claim/result, dispatch claim, then async terminal result
	}
	s, err := New(
		fakeScanner{inventory: inventory},
		receipts,
		driver.NewRegistry(helperDriver{}),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Shutdown()

	start := command(protocol.CommandSessionStart, map[string]any{"working_directory": root})
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}
	dispatch := command(protocol.CommandTaskDispatch, map[string]any{
		"request_id": "request-terminal-save-failure",
		"prompt":     "run",
	})
	result := s.Handle(context.Background(), dispatch)
	if result.Ack.Status != "accepted" {
		t.Fatalf("dispatch should be accepted before the child exits: %#v", result.Ack)
	}

	select {
	case update := <-s.Acks():
		if update.Ack.Code != "receipt_store_error" || update.PersistenceError == nil {
			t.Fatalf("terminal receipt failure was not propagated: %#v", update)
		}
		if receipt := durable.values[receiptKey(dispatch)]; receipt.Status != "accepted" {
			t.Fatalf("durable accepted claim should remain recoverable: %#v", receipt)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for terminal receipt failure")
	}
}

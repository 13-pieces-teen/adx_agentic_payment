package supervisor

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
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
	values  map[string]protocol.CommandAck
	results map[string]protocol.AgentTaskResultEnvelope
}

type failSaveReceipts struct {
	delegate  *memoryReceipts
	saveCalls int
	failAt    int
}

type helperDriver struct {
	kind  string
	tasks chan driver.TaskSpec
}

type arenaSchemaDriver struct{}

func (h helperDriver) Kind() string {
	if h.kind != "" {
		return h.kind
	}
	return "test_runtime"
}

func (h helperDriver) BuildTask(
	ctx context.Context,
	_ protocol.Runtime,
	_ driver.SessionSpec,
	task driver.TaskSpec,
) (*exec.Cmd, error) {
	if h.tasks != nil {
		h.tasks <- task
	}
	process := exec.CommandContext(ctx, os.Args[0], "-test.run=TestSupervisorHelperProcess", "--")
	process.Env = append(os.Environ(), "ADX_CONNECTOR_HELPER_PROCESS=1")
	return process, nil
}

func (arenaSchemaDriver) Kind() string {
	return "test_runtime"
}

func (arenaSchemaDriver) BuildTask(
	ctx context.Context,
	_ protocol.Runtime,
	_ driver.SessionSpec,
	task driver.TaskSpec,
) (*exec.Cmd, error) {
	if task.ArenaKind != "arena.decide" {
		return nil, fmt.Errorf("missing Arena task kind")
	}
	if strings.TrimSpace(task.OutputSchema) == "" || task.OutputSchemaPath == "" {
		return nil, fmt.Errorf("missing Arena output schema")
	}
	if task.IsolatedWorkingDir == "" ||
		filepath.Dir(task.OutputSchemaPath) != task.IsolatedWorkingDir {
		return nil, fmt.Errorf("Arena task is not isolated from the user project")
	}
	schemaFile, err := os.ReadFile(task.OutputSchemaPath)
	if err != nil {
		return nil, fmt.Errorf("read Arena output schema: %w", err)
	}
	var inlineSchema map[string]any
	if err := json.Unmarshal([]byte(task.OutputSchema), &inlineSchema); err != nil {
		return nil, fmt.Errorf("decode inline Arena output schema: %w", err)
	}
	if _, ok := inlineSchema["oneOf"]; !ok {
		return nil, fmt.Errorf("Claude Arena output schema must preserve its union")
	}
	var fileSchema map[string]any
	if err := json.Unmarshal(schemaFile, &fileSchema); err != nil {
		return nil, fmt.Errorf("decode file Arena output schema: %w", err)
	}
	if fileSchema["type"] != "object" || fileSchema["oneOf"] != nil {
		return nil, fmt.Errorf("Codex Arena output schema must use a root object")
	}
	process := exec.CommandContext(ctx, os.Args[0], "-test.run=TestSupervisorHelperProcess", "--")
	process.Env = append(os.Environ(), "ADX_CONNECTOR_HELPER_PROCESS=1")
	return process, nil
}

func TestSupervisorHelperProcess(t *testing.T) {
	if os.Getenv("ADX_CONNECTOR_HELPER_PROCESS") != "1" {
		return
	}
	fmt.Println(`{"session_id":"runtime-session-1","type":"result","result":"{\"action\":\"buy\",\"good\":\"grain\"}"}`)
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

func (m *memoryReceipts) SaveAgentTaskResult(result protocol.AgentTaskResultEnvelope) error {
	if m.results == nil {
		m.results = make(map[string]protocol.AgentTaskResultEnvelope)
	}
	m.results[result.Result.TaskID] = result
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

func (f *failSaveReceipts) SaveAgentTaskResult(result protocol.AgentTaskResultEnvelope) error {
	return f.delegate.SaveAgentTaskResult(result)
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

func arenaDecideTask() map[string]any {
	return map[string]any{
		"taskId":         "task-arena-decide-1",
		"kind":           "arena.decide",
		"schemaVersion":  "arena.agent-task.v1",
		"gameId":         "game-1",
		"roundId":        "round-1",
		"gameAgentId":    "game-agent-1",
		"negotiationId":  nil,
		"deadlineAt":     "2030-07-25T12:00:30Z",
		"idempotencyKey": "game-1:round-1:game-agent-1:decide",
		"inputHash":      "sha256:" + strings.Repeat("0", 64),
		"input": map[string]any{
			"phase":      "decide",
			"gameId":     "game-1",
			"roundId":    "round-1",
			"roundIndex": 1,
			"cash":       "20.000000",
			"holdings":   map[string]any{"grain": 1},
			"market":     map[string]any{"grain": "2.000000"},
			"events":     []any{},
			"reputation": map[string]any{"failedNegotiations": 0},
			"limits": map[string]any{
				"allowedActions": []any{"buy", "sell", "pass"},
				"allowedGoods":   []any{"grain"},
			},
			"completedActions": []any{},
			"completedTrades":  []any{},
			"goods": []any{
				map[string]any{
					"good":               "grain",
					"fixedQuantity":      1,
					"priceDecimalPlaces": 6,
				},
			},
			"deadlineAt": "2030-07-25T12:00:30Z",
		},
	}
}

func TestArenaNegotiationPromptMirrorsAuthoritativeConvergenceRules(t *testing.T) {
	task := arenaDecideTask()
	task["kind"] = "arena.negotiate"
	task["negotiationId"] = "negotiation-1"
	raw, err := json.Marshal(task)
	if err != nil {
		t.Fatal(err)
	}

	_, prompt, err := decodeArenaTask(raw, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{
		"accept an in-bound counterparty quote",
		"propose exactly your limitPrice",
		"reject when remainingTurns <= 1",
	} {
		if !strings.Contains(prompt, expected) {
			t.Fatalf("negotiation prompt omitted %q: %s", expected, prompt)
		}
	}
}

func TestArenaNegotiationActionRequiresStrictlyPositiveFixedPointPrice(t *testing.T) {
	for _, raw := range []string{
		`{"action":"propose","price":"0","message":"offer"}`,
		`{"action":"propose","price":"0.000","message":"offer"}`,
	} {
		if _, err := validateArenaAction("arena.negotiate", []byte(raw)); err == nil {
			t.Fatalf("zero price must be rejected: %s", raw)
		}
	}
	if _, err := validateArenaAction(
		"arena.negotiate",
		[]byte(`{"action":"propose","price":"0.001","message":"offer"}`),
	); err != nil {
		t.Fatalf("positive fixed-point price was rejected: %v", err)
	}
}

func TestArenaActionMatchesSharedWireBounds(t *testing.T) {
	invalid := []struct {
		kind string
		raw  string
	}{
		{
			kind: "arena.decide",
			raw:  `{"action":"buy","good":"bad good"}`,
		},
		{
			kind: "arena.decide",
			raw: fmt.Sprintf(
				`{"action":"sell","good":"%s"}`,
				strings.Repeat("g", 129),
			),
		},
		{
			kind: "arena.decide",
			raw:  `{"action":"buy","good":"grain","quantity":0}`,
		},
		{
			kind: "arena.decide",
			raw:  `{"action":"sell","good":"grain","quantity":1000001}`,
		},
		{
			kind: "arena.decide",
			raw:  `{"action":"buy","good":"grain","limitPrice":"0"}`,
		},
		{
			kind: "arena.negotiate",
			raw:  `{"action":"propose","price":"123456789012345678901234567890123456789","message":"offer"}`,
		},
		{
			kind: "arena.negotiate",
			raw:  `{"action":"propose","price":"1.1234567890123456789","message":"offer"}`,
		},
	}
	for _, testCase := range invalid {
		if _, err := validateArenaAction(
			testCase.kind,
			[]byte(testCase.raw),
		); err == nil {
			t.Fatalf("out-of-contract action must be rejected: %s", testCase.raw)
		}
	}
}

func TestArenaActionAcceptsCurrentQuantityAndLimitPriceWireFields(t *testing.T) {
	raw := []byte(
		`{"action":"buy","good":"grain","quantity":1,"limitPrice":"2.500000"}`,
	)
	action, err := validateArenaAction("arena.decide", raw)
	if err != nil {
		t.Fatal(err)
	}
	if string(action) != string(raw) {
		t.Fatalf("unexpected canonical Arena action: %s", action)
	}
	if _, err := validateArenaAction(
		"arena.decide",
		[]byte(`{"action":"buy","good":"grain","quantity":2}`),
	); err == nil {
		t.Fatal("current Arena game must reject non-unit trade quantities")
	}
}

func TestArenaActionCaptureAcceptsCodexTerminalAgentMessage(t *testing.T) {
	capture := &arenaActionCapture{}
	capture.observe(
		"arena.decide",
		map[string]any{
			"type": "item.completed",
			"item": map[string]any{
				"type": "agent_message",
				"text": `{"action":"pass"}`,
			},
		},
	)
	action, err := capture.terminal()
	if err != nil {
		t.Fatal(err)
	}
	if string(action) != `{"action":"pass"}` {
		t.Fatalf("unexpected Codex Arena action: %s", action)
	}

	nullable := &arenaActionCapture{}
	nullable.observe(
		"arena.decide",
		map[string]any{
			"type": "item.completed",
			"item": map[string]any{
				"type": "agent_message",
				"text": `{"action":"buy","good":"grain","quantity":1,"limitPrice":null}`,
			},
		},
	)
	action, err = nullable.terminal()
	if err != nil {
		t.Fatal(err)
	}
	if string(action) != `{"action":"buy","good":"grain","quantity":1}` {
		t.Fatalf("unexpected normalized Codex Arena action: %s", action)
	}
	if _, err := validateCodexArenaAction(
		"arena.decide",
		[]byte(`{"action":"pass","good":null,"quantity":null,"limitPrice":null,"extra":null}`),
	); err == nil {
		t.Fatal("unknown nullable Codex field must not bypass strict validation")
	}

	toolOutput := &arenaActionCapture{}
	toolOutput.observe(
		"arena.decide",
		map[string]any{
			"type": "item.completed",
			"item": map[string]any{
				"type": "command_execution",
				"text": `{"action":"buy","good":"grain"}`,
			},
		},
	)
	if _, err := toolOutput.terminal(); err == nil {
		t.Fatal("Codex tool output must not be treated as a terminal Arena action")
	}
}

func TestClaudeArenaActionNormalizesBoundedProviderDivergence(t *testing.T) {
	action, err := validateClaudeArenaAction(
		"arena.decide",
		[]byte(`{"action":"buy","good":"gems","quantity":1,"price":"1.800000"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	if string(action) !=
		`{"action":"buy","good":"gems","quantity":1,"limitPrice":"1.800000"}` {
		t.Fatalf("unexpected normalized Claude decide action: %s", action)
	}

	longMessage := strings.Repeat("界", 101)
	action, err = validateClaudeArenaAction(
		"arena.negotiate",
		[]byte(
			`{"action":"propose","price":"6.000000","message":"`+
				longMessage+`"}`,
		),
	)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(action, &fields); err != nil {
		t.Fatal(err)
	}
	if len([]rune(fields["message"].(string))) != 100 {
		t.Fatalf("public message was not bounded: %s", action)
	}

	action, err = validateClaudeArenaAction(
		"arena.negotiate",
		[]byte(
			`{"action":"offer","price":"3.20","message":"Opening offer."}`,
		),
	)
	if err != nil {
		t.Fatal(err)
	}
	if string(action) !=
		`{"action":"propose","price":"3.20","message":"Opening offer."}` {
		t.Fatalf("unexpected normalized Claude negotiation action: %s", action)
	}

	action, err = validateClaudeArenaAction(
		"arena.negotiate",
		[]byte(
			`{"type":"propose","quote":"3.700000","message":"Firm and fair."}`,
		),
	)
	if err != nil {
		t.Fatal(err)
	}
	if string(action) !=
		`{"action":"propose","price":"3.700000","message":"Firm and fair."}` {
		t.Fatalf("unexpected normalized Claude negotiation keys: %s", action)
	}

	action, err = validateClaudeArenaAction(
		"arena.negotiate",
		[]byte(
			`{"action":"accept","price":"3.000000","message":"成交。"}`,
		),
	)
	if err != nil {
		t.Fatal(err)
	}
	if string(action) != `{"action":"accept"}` {
		t.Fatalf("Claude accept must use Arena's frozen quote: %s", action)
	}

	if _, err := validateClaudeArenaAction(
		"arena.decide",
		[]byte(
			`{"action":"buy","good":"gems","price":"1.8","limitPrice":"1.7"}`,
		),
	); err == nil {
		t.Fatal("conflicting Claude price fields must be rejected")
	}
	if _, err := validateClaudeArenaAction(
		"arena.decide",
		[]byte(`{"action":"pass","unexpected":true}`),
	); err == nil {
		t.Fatal("unknown Claude fields must still be rejected")
	}
	if _, err := validateClaudeArenaAction(
		"arena.negotiate",
		[]byte(
			`{"type":"propose","action":"reject","quote":"3","message":"x"}`,
		),
	); err == nil {
		t.Fatal("conflicting Claude negotiation keys must be rejected")
	}
}

func TestArenaActionRejectsTrailingJSONValues(t *testing.T) {
	if _, err := validateArenaAction(
		"arena.decide",
		[]byte(`{"action":"pass"}{"action":"buy","good":"grain"}`),
	); err == nil {
		t.Fatal("multiple JSON values must not be accepted as one Arena action")
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

func TestTypedArenaTaskDispatchDoesNotRequireCloudPrompt(t *testing.T) {
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
	s, err := New(
		fakeScanner{inventory: inventory},
		&memoryReceipts{values: make(map[string]protocol.CommandAck)},
		driver.NewRegistry(helperDriver{}),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	start := command(
		protocol.CommandSessionStart,
		map[string]any{"working_directory": root},
	)
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	task := arenaDecideTask()
	dispatch := command(protocol.CommandTaskDispatch, map[string]any{"task": task})
	dispatch.IdempotencyKey = task["idempotencyKey"].(string)
	result := s.Handle(context.Background(), dispatch)

	if result.Ack.Status != "accepted" {
		t.Fatalf("typed Arena task should start without a cloud prompt: %#v", result.Ack)
	}
	select {
	case <-s.Acks():
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for typed Arena task process")
	}
}

func TestTypedArenaTaskProvidesRuntimeWithStrictOutputSchema(t *testing.T) {
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
	s, err := New(
		fakeScanner{inventory: inventory},
		&memoryReceipts{values: make(map[string]protocol.CommandAck)},
		driver.NewRegistry(arenaSchemaDriver{}),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	start := command(
		protocol.CommandSessionStart,
		map[string]any{"working_directory": root},
	)
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	task := arenaDecideTask()
	dispatch := command(protocol.CommandTaskDispatch, map[string]any{"task": task})
	dispatch.IdempotencyKey = task["idempotencyKey"].(string)
	result := s.Handle(context.Background(), dispatch)
	if result.Ack.Status != "accepted" {
		t.Fatalf("typed Arena task should have a strict output schema: %#v", result.Ack)
	}
}

func TestTypedArenaTaskRejectsLocalRuntimeThatIsNotExecutionReady(t *testing.T) {
	root := t.TempDir()
	inventory := protocol.InventorySnapshot{
		ObservedAt: time.Now().UTC(),
		Runtimes: []protocol.Runtime{{
			ID:                   "runtime-1",
			Kind:                 "codex",
			ExecutablePath:       os.Args[0],
			Status:               "ready",
			Available:            true,
			TaskEnabled:          true,
			AuthenticationStatus: "configured",
			ArenaCompatible:      false,
			ArenaIsolation:       "read_only_ephemeral_schema",
			LocalExecutionReady:  true,
		}},
	}
	s, err := New(
		fakeScanner{inventory: inventory},
		&memoryReceipts{values: make(map[string]protocol.CommandAck)},
		driver.NewRegistry(helperDriver{kind: "codex"}),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	start := command(
		protocol.CommandSessionStart,
		map[string]any{"working_directory": root},
	)
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	task := arenaDecideTask()
	dispatch := command(protocol.CommandTaskDispatch, map[string]any{"task": task})
	dispatch.IdempotencyKey = task["idempotencyKey"].(string)
	result := s.Handle(context.Background(), dispatch)
	if result.Ack.Status != "rejected" || result.Ack.Code != "runtime_not_arena_ready" {
		t.Fatalf("unready local Runtime must fail closed: %#v", result.Ack)
	}
}

func TestTypedArenaTaskRetriesInterruptedReceiptWithNewCommand(t *testing.T) {
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
	durable := &memoryReceipts{
		values:  make(map[string]protocol.CommandAck),
		results: make(map[string]protocol.AgentTaskResultEnvelope),
	}
	s, err := New(
		fakeScanner{inventory: inventory},
		durable,
		driver.NewRegistry(helperDriver{}),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	start := command(
		protocol.CommandSessionStart,
		map[string]any{"working_directory": root},
	)
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	task := arenaDecideTask()
	interrupted := command(
		protocol.CommandTaskDispatch,
		map[string]any{"task": task},
	)
	interrupted.CommandID = "cmd-before-connector-restart"
	interrupted.IdempotencyKey = task["idempotencyKey"].(string)
	durable.values[receiptKey(interrupted)] = protocol.CommandAck{
		CommandID:      interrupted.CommandID,
		IdempotencyKey: interrupted.IdempotencyKey,
		BindingEpoch:   interrupted.BindingEpoch,
		Status:         "failed",
		Code:           "connector_restarted",
		RecordedAt:     time.Now().UTC(),
	}

	retry := command(protocol.CommandTaskDispatch, map[string]any{"task": task})
	retry.IdempotencyKey = task["idempotencyKey"].(string)
	result := s.Handle(context.Background(), retry)
	if result.Ack.Status != "accepted" ||
		result.Ack.CommandID != retry.CommandID {
		t.Fatalf("interrupted Arena task was not retried: %#v", result.Ack)
	}

	select {
	case terminal := <-s.Results():
		if terminal.Result.TaskID != task["taskId"] {
			t.Fatalf("unexpected retried Arena result: %#v", terminal)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for retried Arena task result")
	}
}

func TestTypedArenaTaskEmitsIndependentTerminalResult(t *testing.T) {
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
	durable := &memoryReceipts{
		values:  make(map[string]protocol.CommandAck),
		results: make(map[string]protocol.AgentTaskResultEnvelope),
	}
	s, err := New(
		fakeScanner{inventory: inventory},
		durable,
		driver.NewRegistry(helperDriver{}),
		[]string{root},
		nil,
		inventory,
	)
	if err != nil {
		t.Fatal(err)
	}
	start := command(
		protocol.CommandSessionStart,
		map[string]any{"working_directory": root},
	)
	if result := s.Handle(context.Background(), start); result.Ack.Status != "completed" {
		t.Fatalf("start failed: %#v", result.Ack)
	}

	task := arenaDecideTask()
	dispatch := command(protocol.CommandTaskDispatch, map[string]any{"task": task})
	dispatch.IdempotencyKey = task["idempotencyKey"].(string)
	if result := s.Handle(context.Background(), dispatch); result.Ack.Status != "accepted" {
		t.Fatalf("typed Arena task was not accepted: %#v", result.Ack)
	}

	select {
	case terminal := <-s.Results():
		persisted, found := durable.results[terminal.Result.TaskID]
		if !found || persisted.Result.ResultID != terminal.Result.ResultID {
			t.Fatalf("terminal result was emitted before durable persistence: %#v", durable.results)
		}
		if terminal.BindingID != dispatch.BindingID ||
			terminal.BindingEpoch != dispatch.BindingEpoch {
			t.Fatalf("terminal result lost its frozen binding: %#v", terminal)
		}
		if terminal.Result.TaskID != task["taskId"] ||
			terminal.Result.SchemaVersion != "arena.agent-result.v1" ||
			terminal.Result.Status != "succeeded" {
			t.Fatalf("unexpected terminal result: %#v", terminal)
		}
		var action map[string]any
		if err := json.Unmarshal(terminal.Result.Action, &action); err != nil {
			t.Fatal(err)
		}
		if action["action"] != "buy" || action["good"] != "grain" {
			t.Fatalf("unexpected Arena action: %#v", action)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for independent terminal Arena result")
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

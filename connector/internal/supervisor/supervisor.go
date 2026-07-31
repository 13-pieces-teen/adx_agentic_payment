package supervisor

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/driver"
	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
	"github.com/adx-agentic-payment/adx/connector/internal/redact"
)

const (
	maxTaskTimeout = time.Hour
	defaultTimeout = 15 * time.Minute
)

type Scanner interface {
	Scan(context.Context) protocol.InventorySnapshot
}

type ReceiptStore interface {
	LookupReceipt(string) (protocol.CommandAck, bool, error)
	SaveReceipt(string, protocol.CommandAck) error
	SaveAgentTaskResult(protocol.AgentTaskResultEnvelope) error
}

type session struct {
	ID                  string
	BindingID           string
	AgentID             string
	RuntimeID           string
	BindingEpoch        uint64
	WorkingDir          string
	ResumeToken         string
	ResumeTokenCaptured bool
	Environment         []string
	Status              string
	Tasks               map[string]*task
}

type task struct {
	ID          string
	command     *exec.Cmd
	containment *processContainment
	origin      protocol.Command
	trackAck    bool
	cancel      context.CancelFunc
	context     context.Context
	cancelled   atomic.Bool
	arenaTask   *arenaTaskEnvelope
	arenaAction *arenaActionCapture
	cleanup     func()
}

type CommandUpdate struct {
	Command          protocol.Command
	Ack              protocol.CommandAck
	PersistenceError error
}

type Supervisor struct {
	mu                 sync.RWMutex
	scanner            Scanner
	receipts           ReceiptStore
	drivers            *driver.Registry
	allowedRoots       []string
	allowedEnvironment map[string]struct{}
	inventory          protocol.InventorySnapshot
	runtimes           map[string]protocol.Runtime
	sessions           map[string]*session
	events             chan protocol.RuntimeEvent
	acks               chan CommandUpdate
	results            chan protocol.AgentTaskResultEnvelope
}

type HandleResult struct {
	Ack              protocol.CommandAck
	Inventory        *protocol.InventorySnapshot
	PersistenceError error
}

type startSessionPayload struct {
	WorkingDirectory string   `json:"working_directory"`
	InitialPrompt    string   `json:"initial_prompt,omitempty"`
	EnvironmentRefs  []string `json:"environment_refs,omitempty"`
}

type dispatchTaskPayload struct {
	TaskID         string          `json:"task_id"`
	RequestID      string          `json:"request_id,omitempty"`
	Prompt         string          `json:"prompt"`
	TimeoutSeconds int             `json:"timeout_seconds,omitempty"`
	Task           json.RawMessage `json:"task,omitempty"`
}

type cancelTaskPayload struct {
	TaskID    string `json:"task_id"`
	RequestID string `json:"request_id,omitempty"`
}

var environmentNamePattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

func New(
	scanner Scanner,
	receipts ReceiptStore,
	drivers *driver.Registry,
	allowedRoots []string,
	allowedEnvironment []string,
	inventory protocol.InventorySnapshot,
) (*Supervisor, error) {
	if scanner == nil {
		return nil, errors.New("runtime scanner is required")
	}
	if receipts == nil {
		return nil, errors.New("receipt store is required")
	}
	if drivers == nil {
		return nil, errors.New("driver registry is required")
	}
	roots, err := normalizeRoots(allowedRoots)
	if err != nil {
		return nil, err
	}
	s := &Supervisor{
		scanner:            scanner,
		receipts:           receipts,
		drivers:            drivers,
		allowedRoots:       roots,
		allowedEnvironment: normalizeEnvironmentAllowlist(allowedEnvironment),
		sessions:           make(map[string]*session),
		events:             make(chan protocol.RuntimeEvent, 8192),
		acks:               make(chan CommandUpdate, 1024),
		results:            make(chan protocol.AgentTaskResultEnvelope, 1024),
	}
	s.replaceInventory(inventory)
	return s, nil
}

func (s *Supervisor) Events() <-chan protocol.RuntimeEvent {
	return s.events
}

func (s *Supervisor) Acks() <-chan CommandUpdate {
	return s.acks
}

func (s *Supervisor) Results() <-chan protocol.AgentTaskResultEnvelope {
	return s.results
}

func (s *Supervisor) RequeueAck(update CommandUpdate) {
	s.acks <- update
}

func (s *Supervisor) Inventory() protocol.InventorySnapshot {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return copyInventory(s.inventory)
}

func (s *Supervisor) Probe(ctx context.Context) protocol.InventorySnapshot {
	inventory := s.scanner.Scan(ctx)
	s.replaceInventory(inventory)
	return inventory
}

func (s *Supervisor) Health() (int, int) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	sessions := 0
	tasks := 0
	for _, current := range s.sessions {
		if current.Status != "stopped" {
			sessions++
		}
		tasks += len(current.Tasks)
	}
	return sessions, tasks
}

func (s *Supervisor) Handle(ctx context.Context, command protocol.Command) HandleResult {
	if command.IdempotencyKey != "" {
		receipt, found, err := s.receipts.LookupReceipt(receiptKey(command))
		if err != nil {
			return HandleResult{
				Ack: protocol.NewAck(
					command,
					"rejected",
					"receipt_store_error",
					"could not read the durable command receipt store",
					nil,
				),
				PersistenceError: fmt.Errorf("lookup durable command receipt: %w", err),
			}
		}
		if found && !canRetryInterruptedArenaTask(command, receipt) {
			return HandleResult{Ack: receipt}
		}
	}
	if command.CommandKind() == protocol.CommandSessionStart && command.SessionID == "" {
		command.SessionID = protocol.NewID("session")
	}
	if err := command.Validate(time.Now().UTC()); err != nil {
		return s.record(command, protocol.NewAck(command, "rejected", "invalid_command", err.Error(), nil), nil)
	}
	claim := protocol.NewAck(command, "accepted", "", "command durably claimed", nil)
	if err := s.receipts.SaveReceipt(receiptKey(command), claim); err != nil {
		return HandleResult{
			Ack: protocol.NewAck(
				command,
				"rejected",
				"receipt_store_error",
				"could not durably claim command before execution",
				nil,
			),
			PersistenceError: fmt.Errorf("claim durable command receipt: %w", err),
		}
	}

	switch command.CommandKind() {
	case protocol.CommandRuntimeProbe:
		inventory := s.scanner.Scan(ctx)
		s.replaceInventory(inventory)
		ack := protocol.NewAck(command, "completed", "", "runtime inventory refreshed", map[string]any{
			"runtime_count": len(inventory.Runtimes),
		})
		return s.record(command, ack, &inventory)
	case protocol.CommandSessionStart:
		return s.record(command, s.startSession(command), nil)
	case protocol.CommandTaskDispatch:
		ack := s.dispatchTask(command, true)
		if ack.Status == "accepted" {
			return HandleResult{Ack: ack}
		}
		return s.record(command, ack, nil)
	case protocol.CommandTaskCancel:
		return s.record(command, s.cancelTask(command), nil)
	case protocol.CommandSessionStop:
		return s.record(command, s.stopSession(command), nil)
	case protocol.CommandSessionResume:
		return s.record(command, s.resumeSession(command), nil)
	default:
		return s.record(
			command,
			protocol.NewAck(command, "rejected", "unsupported_command", "command is not in the connector allowlist", nil),
			nil,
		)
	}
}

func (s *Supervisor) Shutdown() {
	s.mu.Lock()
	tasks := make([]*task, 0)
	for _, current := range s.sessions {
		current.Status = "stopped"
		for _, running := range current.Tasks {
			tasks = append(tasks, running)
		}
	}
	s.mu.Unlock()
	for _, running := range tasks {
		stopTask(running)
	}
}

func (s *Supervisor) startSession(command protocol.Command) protocol.CommandAck {
	var payload startSessionPayload
	if err := json.Unmarshal(command.Payload, &payload); err != nil {
		return protocol.NewAck(command, "rejected", "invalid_payload", err.Error(), nil)
	}
	if err := rejectCloudResumeToken(command.Payload); err != nil {
		return protocol.NewAck(command, "rejected", "cloud_resume_token_forbidden", err.Error(), nil)
	}
	if strings.TrimSpace(payload.WorkingDirectory) == "" {
		return protocol.NewAck(command, "rejected", "invalid_payload", "working_directory is required", nil)
	}
	workingDirectory, err := s.validateWorkingDirectory(payload.WorkingDirectory)
	if err != nil {
		return protocol.NewAck(command, "rejected", "working_directory_denied", err.Error(), nil)
	}

	environment, err := buildEnvironment(payload.EnvironmentRefs, s.allowedEnvironment)
	if err != nil {
		return protocol.NewAck(command, "rejected", "invalid_environment_refs", err.Error(), nil)
	}
	if len(payload.InitialPrompt) > 1_000_000 {
		return protocol.NewAck(command, "rejected", "invalid_payload", "initial_prompt exceeds 1000000 bytes", nil)
	}

	s.mu.Lock()
	if existing, exists := s.sessions[command.SessionID]; exists {
		if code, message := validateSessionOwnership(existing, command); code != "" {
			s.mu.Unlock()
			return protocol.NewAck(command, "rejected", code, message, nil)
		}
		if existing.Status != "stopped" {
			s.mu.Unlock()
			return protocol.NewAck(command, "rejected", "session_exists", "session is already active", nil)
		}
	}
	runtimeInfo, found := s.runtimes[command.RuntimeID]
	if !found {
		s.mu.Unlock()
		return protocol.NewAck(command, "rejected", "runtime_not_found", "runtime is not present in the latest inventory", nil)
	}
	if runtimeInfo.Status != "ready" {
		s.mu.Unlock()
		return protocol.NewAck(command, "rejected", "runtime_not_ready", runtimeInfo.StatusDetail, nil)
	}
	s.sessions[command.SessionID] = &session{
		ID:           command.SessionID,
		BindingID:    command.BindingID,
		AgentID:      command.AgentID,
		RuntimeID:    command.RuntimeID,
		BindingEpoch: command.BindingEpoch,
		WorkingDir:   workingDirectory,
		Environment:  environment,
		Status:       "ready",
		Tasks:        make(map[string]*task),
	}
	s.mu.Unlock()
	s.emit(protocol.NewRuntimeEvent(
		command.RuntimeID,
		command.SessionID,
		"",
		"session.started",
		map[string]any{"working_directory": workingDirectory, "binding_epoch": command.BindingEpoch},
	))
	if strings.TrimSpace(payload.InitialPrompt) != "" {
		initialTaskID := command.CommandID + "-initial"
		dispatchPayload, _ := json.Marshal(dispatchTaskPayload{
			TaskID: initialTaskID,
			Prompt: payload.InitialPrompt,
		})
		initialCommand := command
		initialCommand.Kind = protocol.CommandTaskDispatch
		initialCommand.Action = ""
		initialCommand.Payload = dispatchPayload
		initialAck := s.dispatchTask(initialCommand, false)
		if initialAck.Status == "rejected" {
			return protocol.NewAck(command, "rejected", initialAck.Code, "session created but initial task failed: "+initialAck.Message, nil)
		}
		return protocol.NewAck(command, "completed", "", "managed session and initial task started", map[string]any{
			"session_id": command.SessionID,
			"task_id":    initialTaskID,
		})
	}
	return protocol.NewAck(command, "completed", "", "managed session created", map[string]any{"session_id": command.SessionID})
}

func (s *Supervisor) dispatchTask(command protocol.Command, trackAck bool) protocol.CommandAck {
	var payload dispatchTaskPayload
	if err := json.Unmarshal(command.Payload, &payload); err != nil {
		return protocol.NewAck(command, "rejected", "invalid_payload", err.Error(), nil)
	}
	typedArenaTask := len(payload.Task) != 0
	var arenaTask *arenaTaskEnvelope
	timeout := defaultTimeout
	if typedArenaTask {
		if payload.TaskID != "" || payload.RequestID != "" ||
			strings.TrimSpace(payload.Prompt) != "" || payload.TimeoutSeconds != 0 {
			return protocol.NewAck(
				command,
				"rejected",
				"invalid_payload",
				"typed Arena task cannot include prompt, request_id, task_id, or timeout_seconds",
				nil,
			)
		}
		task, prompt, err := decodeArenaTask(payload.Task, time.Now().UTC())
		if err != nil {
			return protocol.NewAck(command, "rejected", "invalid_arena_task", err.Error(), nil)
		}
		if task.IdempotencyKey != command.IdempotencyKey {
			return protocol.NewAck(
				command,
				"rejected",
				"idempotency_mismatch",
				"Arena task idempotencyKey does not match the Connector command",
				nil,
			)
		}
		arenaTask = &task
		payload.TaskID = task.TaskID
		payload.Prompt = prompt
		remaining := time.Until(task.DeadlineAt)
		if remaining < timeout {
			timeout = remaining
		}
	} else {
		if payload.TaskID == "" {
			payload.TaskID = payload.RequestID
		}
		if payload.TaskID == "" || strings.TrimSpace(payload.Prompt) == "" {
			return protocol.NewAck(command, "rejected", "invalid_payload", "task_id and prompt are required", nil)
		}
		if payload.TimeoutSeconds != 0 {
			timeout = time.Duration(payload.TimeoutSeconds) * time.Second
		}
		if timeout <= 0 || timeout > maxTaskTimeout {
			return protocol.NewAck(command, "rejected", "invalid_timeout", "timeout_seconds must be between 1 and 3600", nil)
		}
	}

	s.mu.RLock()
	current, found := s.sessions[command.SessionID]
	if !found {
		s.mu.RUnlock()
		return protocol.NewAck(command, "rejected", "session_not_found", "managed session does not exist", nil)
	}
	if code, message := validateSessionOwnership(current, command); code != "" {
		s.mu.RUnlock()
		return protocol.NewAck(command, "rejected", code, message, nil)
	}
	if current.Status != "ready" {
		s.mu.RUnlock()
		return protocol.NewAck(command, "rejected", "session_not_ready", "session is stopped", nil)
	}
	if _, duplicate := current.Tasks[payload.TaskID]; duplicate {
		s.mu.RUnlock()
		return protocol.NewAck(command, "rejected", "task_exists", "task is already running", nil)
	}
	runtimeInfo, runtimeFound := s.runtimes[current.RuntimeID]
	sessionSpec := driver.SessionSpec{
		SessionID:   current.ID,
		WorkingDir:  current.WorkingDir,
		ResumeToken: current.ResumeToken,
		Environment: append([]string(nil), current.Environment...),
	}
	if typedArenaTask {
		// Arena reconstructs all game context in the immutable task snapshot.
		// A provider session is not a business recovery authority.
		sessionSpec.ResumeToken = ""
	}
	s.mu.RUnlock()
	if !runtimeFound {
		return protocol.NewAck(command, "rejected", "runtime_not_found", "session runtime is no longer available", nil)
	}
	if typedArenaTask &&
		(runtimeInfo.Kind == "codex" || runtimeInfo.Kind == "claude_code") &&
		!runtimeIsArenaReady(runtimeInfo) {
		return protocol.NewAck(
			command,
			"rejected",
			"runtime_not_arena_ready",
			"local Runtime has not passed task, authentication, and Arena isolation readiness checks",
			nil,
		)
	}
	runtimeDriver, supported := s.drivers.Driver(runtimeInfo.Kind)
	if !supported {
		return protocol.NewAck(command, "rejected", "driver_not_supported", "no managed driver exists for this runtime", nil)
	}

	taskContext, cancel := context.WithTimeout(context.Background(), timeout)
	taskSpec := driver.TaskSpec{
		TaskID: payload.TaskID,
		Prompt: payload.Prompt,
	}
	cleanup := func() {}
	if arenaTask != nil {
		outputSchema, outputSchemaPath, isolatedWorkingDirectory, schemaCleanup, schemaErr :=
			prepareArenaOutputSchema(arenaTask.Kind)
		if schemaErr != nil {
			cancel()
			return protocol.NewAck(command, "rejected", "driver_error", schemaErr.Error(), nil)
		}
		taskSpec.ArenaKind = arenaTask.Kind
		taskSpec.OutputSchema = outputSchema
		taskSpec.OutputSchemaPath = outputSchemaPath
		taskSpec.IsolatedWorkingDir = isolatedWorkingDirectory
		cleanup = schemaCleanup
	}
	process, err := runtimeDriver.BuildTask(taskContext, runtimeInfo, sessionSpec, taskSpec)
	if err != nil {
		cancel()
		cleanup()
		return protocol.NewAck(command, "rejected", "driver_error", err.Error(), nil)
	}
	stdout, err := process.StdoutPipe()
	if err != nil {
		cancel()
		cleanup()
		return protocol.NewAck(command, "rejected", "process_pipe_error", err.Error(), nil)
	}
	stderr, err := process.StderrPipe()
	if err != nil {
		cancel()
		cleanup()
		return protocol.NewAck(command, "rejected", "process_pipe_error", err.Error(), nil)
	}
	containment, err := startManagedProcess(process)
	if err != nil {
		cancel()
		cleanup()
		return protocol.NewAck(command, "rejected", "process_start_error", redact.Text(err.Error()), nil)
	}
	running := &task{
		ID:          payload.TaskID,
		command:     process,
		containment: containment,
		origin:      command,
		trackAck:    trackAck,
		cancel:      cancel,
		context:     taskContext,
		arenaTask:   arenaTask,
		cleanup:     cleanup,
	}
	if arenaTask != nil {
		running.arenaAction = &arenaActionCapture{}
	}

	s.mu.Lock()
	current, found = s.sessions[command.SessionID]
	if !found || current.Status != "ready" {
		s.mu.Unlock()
		stopTask(running)
		return protocol.NewAck(command, "rejected", "session_changed", "session changed while the process was starting", nil)
	}
	if code, _ := validateSessionOwnership(current, command); code != "" {
		s.mu.Unlock()
		stopTask(running)
		return protocol.NewAck(command, "rejected", "session_changed", "session ownership changed while the process was starting", nil)
	}
	current.Tasks[payload.TaskID] = running
	s.mu.Unlock()

	s.emit(protocol.NewRuntimeEvent(
		runtimeInfo.ID,
		command.SessionID,
		payload.TaskID,
		"process.started",
		map[string]any{
			"command_id":      command.CommandID,
			"pid":             process.Process.Pid,
			"timeout_seconds": int(timeout.Seconds()),
		},
	))
	ack := protocol.NewAck(command, "accepted", "", "task process started", map[string]any{
		"task_id": payload.TaskID,
		"pid":     process.Process.Pid,
	})
	go s.observeTask(runtimeInfo.ID, command.SessionID, running, stdout, stderr)
	return ack
}

func (s *Supervisor) cancelTask(command protocol.Command) protocol.CommandAck {
	var payload cancelTaskPayload
	if err := json.Unmarshal(command.Payload, &payload); err != nil {
		return protocol.NewAck(command, "rejected", "invalid_payload", err.Error(), nil)
	}
	if payload.TaskID == "" {
		payload.TaskID = payload.RequestID
	}
	if payload.TaskID == "" {
		return protocol.NewAck(command, "rejected", "invalid_payload", "task_id is required", nil)
	}
	s.mu.RLock()
	current, found := s.sessions[command.SessionID]
	if !found {
		s.mu.RUnlock()
		return protocol.NewAck(command, "rejected", "session_not_found", "managed session does not exist", nil)
	}
	if code, message := validateSessionOwnership(current, command); code != "" {
		s.mu.RUnlock()
		return protocol.NewAck(command, "rejected", code, message, nil)
	}
	running, found := current.Tasks[payload.TaskID]
	s.mu.RUnlock()
	if !found {
		return protocol.NewAck(command, "rejected", "task_not_found", "running task does not exist", nil)
	}
	stopTask(running)
	return protocol.NewAck(command, "completed", "", "task cancellation signal delivered", nil)
}

func (s *Supervisor) stopSession(command protocol.Command) protocol.CommandAck {
	s.mu.Lock()
	current, found := s.sessions[command.SessionID]
	if !found {
		s.mu.Unlock()
		return protocol.NewAck(command, "rejected", "session_not_found", "managed session does not exist", nil)
	}
	if code, message := validateSessionOwnership(current, command); code != "" {
		s.mu.Unlock()
		return protocol.NewAck(command, "rejected", code, message, nil)
	}
	current.Status = "stopped"
	tasks := make([]*task, 0, len(current.Tasks))
	for _, running := range current.Tasks {
		tasks = append(tasks, running)
	}
	runtimeID := current.RuntimeID
	s.mu.Unlock()
	for _, running := range tasks {
		stopTask(running)
	}
	s.emit(protocol.NewRuntimeEvent(runtimeID, command.SessionID, "", "session.stopped", nil))
	return protocol.NewAck(command, "completed", "", "managed session stopped", nil)
}

func (s *Supervisor) resumeSession(command protocol.Command) protocol.CommandAck {
	if err := rejectCloudResumeToken(command.Payload); err != nil {
		return protocol.NewAck(command, "rejected", "cloud_resume_token_forbidden", err.Error(), nil)
	}
	s.mu.Lock()
	current, found := s.sessions[command.SessionID]
	if !found {
		s.mu.Unlock()
		return protocol.NewAck(command, "rejected", "session_not_found", "managed session does not exist", nil)
	}
	if code, message := validateSessionOwnership(current, command); code != "" {
		s.mu.Unlock()
		return protocol.NewAck(command, "rejected", code, message, nil)
	}
	if current.Status != "stopped" {
		s.mu.Unlock()
		return protocol.NewAck(command, "rejected", "session_not_stopped", "managed session must be stopped before it can be resumed", nil)
	}
	if !current.ResumeTokenCaptured || current.ResumeToken == "" {
		s.mu.Unlock()
		return protocol.NewAck(command, "rejected", "resume_token_unavailable", "the Connector-owned session has not captured a provider resume token", nil)
	}
	current.Status = "ready"
	runtimeID := current.RuntimeID
	s.mu.Unlock()
	s.emit(protocol.NewRuntimeEvent(runtimeID, command.SessionID, "", "session.resumed", map[string]any{
		"runtime_resume_available": true,
	}))
	return protocol.NewAck(command, "completed", "", "managed session resumed", map[string]any{
		"runtime_resume_available": true,
	})
}

func (s *Supervisor) observeTask(
	runtimeID string,
	sessionID string,
	running *task,
	stdout io.Reader,
	stderr io.Reader,
) {
	if running.cleanup != nil {
		defer running.cleanup()
	}
	var streams sync.WaitGroup
	streams.Add(2)
	go func() {
		defer streams.Done()
		s.streamOutput(runtimeID, sessionID, running, "stdout", stdout)
	}()
	go func() {
		defer streams.Done()
		s.streamOutput(runtimeID, sessionID, running, "stderr", stderr)
	}()
	waitErr := running.command.Wait()
	releaseProcess(running.containment)
	streams.Wait()
	contextErr := running.context.Err()
	running.cancel()

	s.mu.Lock()
	if current, found := s.sessions[sessionID]; found {
		delete(current.Tasks, running.ID)
	}
	s.mu.Unlock()

	data := map[string]any{
		"cancelled":  running.cancelled.Load(),
		"command_id": running.origin.CommandID,
	}
	exitCode := 0
	if waitErr != nil {
		data["error"] = redact.Text(waitErr.Error())
		exitCode = -1
		if exitError, ok := waitErr.(*exec.ExitError); ok {
			exitCode = exitError.ExitCode()
		}
	}
	data["exit_code"] = exitCode
	if errors.Is(contextErr, context.DeadlineExceeded) {
		data["timed_out"] = true
	}
	s.emit(protocol.NewRuntimeEvent(runtimeID, sessionID, running.ID, "process.exited", data))

	var taskResult *protocol.AgentTaskResultEnvelope
	if running.arenaTask != nil {
		status := "succeeded"
		var action json.RawMessage
		if running.cancelled.Load() {
			status = "cancelled"
		} else if errors.Is(contextErr, context.DeadlineExceeded) {
			status = "timed_out"
		} else if waitErr != nil {
			status = "failed"
		} else {
			action, _ = running.arenaAction.terminal()
			if len(action) == 0 {
				status = "failed"
			}
		}
		taskResult = &protocol.AgentTaskResultEnvelope{
			BindingID:    running.origin.BindingID,
			BindingEpoch: running.origin.BindingEpoch,
			Result: protocol.AgentTaskResult{
				SchemaVersion: "arena.agent-result.v1",
				ResultID: protocol.NewAgentTaskResultID(
					running.origin.BindingID,
					running.ID,
					running.origin.IdempotencyKey,
				),
				TaskID: running.ID,
				Status: status,
				Action: action,
			},
		}
		if err := s.receipts.SaveAgentTaskResult(*taskResult); err != nil {
			finalAck := protocol.NewAck(
				running.origin,
				"failed",
				"result_store_error",
				"task exited but its Arena result could not be persisted",
				map[string]any{"task_id": running.ID, "exit_code": exitCode},
			)
			s.acks <- CommandUpdate{
				Command:          running.origin,
				Ack:              finalAck,
				PersistenceError: fmt.Errorf("persist terminal Arena task result: %w", err),
			}
			return
		}
	}

	if running.trackAck {
		status := "completed"
		code := ""
		message := "task process exited successfully"
		if waitErr != nil {
			status = "failed"
			code = "process_exit_error"
			message = redact.Text(waitErr.Error())
		}
		if running.cancelled.Load() {
			status = "failed"
			code = "task_cancelled"
			message = "task process was cancelled"
		}
		if errors.Is(contextErr, context.DeadlineExceeded) {
			status = "failed"
			code = "task_timed_out"
			message = "task process exceeded its timeout"
		}
		finalAck := protocol.NewAck(running.origin, status, code, message, map[string]any{
			"task_id":   running.ID,
			"exit_code": exitCode,
			"cancelled": running.cancelled.Load(),
			"timed_out": errors.Is(contextErr, context.DeadlineExceeded),
		})
		if err := s.receipts.SaveReceipt(receiptKey(running.origin), finalAck); err != nil {
			finalAck = protocol.NewAck(
				running.origin,
				"failed",
				"receipt_store_error",
				"task exited but its terminal receipt could not be persisted",
				map[string]any{"task_id": running.ID, "exit_code": exitCode},
			)
			s.acks <- CommandUpdate{
				Command:          running.origin,
				Ack:              finalAck,
				PersistenceError: fmt.Errorf("persist terminal task receipt: %w", err),
			}
			return
		}
		s.acks <- CommandUpdate{Command: running.origin, Ack: finalAck}
	}
	if taskResult != nil {
		s.results <- *taskResult
	}
}

func (s *Supervisor) streamOutput(
	runtimeID string,
	sessionID string,
	running *task,
	stream string,
	reader io.Reader,
) {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		var structured map[string]any
		if json.Unmarshal([]byte(line), &structured) == nil {
			s.captureResumeToken(sessionID, structured)
			if running.arenaTask != nil && stream == "stdout" {
				running.arenaAction.observe(running.arenaTask.Kind, structured)
			}
			redacted, _ := redact.Value(structured).(map[string]any)
			s.emit(protocol.NewRuntimeEvent(runtimeID, sessionID, running.ID, "runtime.message", map[string]any{
				"stream":  stream,
				"message": redacted,
			}))
			continue
		}
		s.emit(protocol.NewRuntimeEvent(runtimeID, sessionID, running.ID, "runtime."+stream, map[string]any{
			"text": redact.Text(line),
		}))
	}
	if err := scanner.Err(); err != nil {
		s.emit(protocol.NewRuntimeEvent(runtimeID, sessionID, running.ID, "runtime.stream_error", map[string]any{
			"stream": stream,
			"error":  redact.Text(err.Error()),
		}))
	}
}

func (s *Supervisor) captureResumeToken(sessionID string, message map[string]any) {
	var token string
	for _, key := range []string{"session_id", "thread_id"} {
		if value, ok := message[key].(string); ok && value != "" {
			token = value
			break
		}
	}
	if token == "" {
		return
	}
	s.mu.Lock()
	if current, found := s.sessions[sessionID]; found {
		if !current.ResumeTokenCaptured {
			current.ResumeToken = token
			current.ResumeTokenCaptured = true
		}
	}
	s.mu.Unlock()
}

func rejectCloudResumeToken(raw json.RawMessage) error {
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(raw, &payload); err != nil {
		return err
	}
	for _, field := range []string{"conversation_id", "resume_token"} {
		if _, supplied := payload[field]; supplied {
			return fmt.Errorf("%s cannot be supplied by the cloud control plane", field)
		}
	}
	return nil
}

func validateSessionOwnership(current *session, command protocol.Command) (string, string) {
	if current.BindingEpoch != command.BindingEpoch {
		return "stale_binding", "binding epoch does not match the active session"
	}
	if current.BindingID != command.BindingID ||
		current.AgentID != command.AgentID ||
		current.RuntimeID != command.RuntimeID {
		return "session_ownership_mismatch", "binding, agent, or runtime does not own the managed session"
	}
	return "", ""
}

func (s *Supervisor) validateWorkingDirectory(path string) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", errors.New("working_directory is required")
	}
	resolved, err := resolvePath(path)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", fmt.Errorf("stat working directory: %w", err)
	}
	if !info.IsDir() {
		return "", errors.New("working_directory is not a directory")
	}
	for _, root := range s.allowedRoots {
		if pathWithin(root, resolved) {
			return resolved, nil
		}
	}
	return "", errors.New("working_directory is outside the connector allowlist")
}

func (s *Supervisor) replaceInventory(inventory protocol.InventorySnapshot) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.inventory = copyInventory(inventory)
	s.runtimes = make(map[string]protocol.Runtime, len(inventory.Runtimes))
	for _, runtimeInfo := range inventory.Runtimes {
		s.runtimes[runtimeInfo.ID] = runtimeInfo
	}
}

func (s *Supervisor) record(
	command protocol.Command,
	ack protocol.CommandAck,
	inventory *protocol.InventorySnapshot,
) HandleResult {
	if command.IdempotencyKey != "" {
		if err := s.receipts.SaveReceipt(receiptKey(command), ack); err != nil {
			ack = protocol.NewAck(command, "rejected", "receipt_store_error", "could not persist command receipt", nil)
			return HandleResult{
				Ack:              ack,
				Inventory:        inventory,
				PersistenceError: fmt.Errorf("persist command receipt: %w", err),
			}
		}
	}
	return HandleResult{Ack: ack, Inventory: inventory}
}

func receiptKey(command protocol.Command) string {
	return command.BindingID + "\x1f" + command.IdempotencyKey
}

func canRetryInterruptedArenaTask(
	command protocol.Command,
	receipt protocol.CommandAck,
) bool {
	if command.CommandKind() != protocol.CommandTaskDispatch ||
		receipt.Status != "failed" ||
		receipt.Code != "connector_restarted" ||
		receipt.CommandID == command.CommandID {
		return false
	}
	var payload dispatchTaskPayload
	if err := json.Unmarshal(command.Payload, &payload); err != nil {
		return false
	}
	return len(payload.Task) != 0
}

func runtimeIsArenaReady(runtimeInfo protocol.Runtime) bool {
	expectedIsolation := ""
	switch runtimeInfo.Kind {
	case "codex":
		expectedIsolation = "read_only_ephemeral_schema"
	case "claude_code":
		expectedIsolation = "no_tools_safe_mode_schema"
	}
	return runtimeInfo.TaskEnabled &&
		runtimeInfo.AuthenticationStatus == "configured" &&
		runtimeInfo.ArenaCompatible &&
		expectedIsolation != "" &&
		runtimeInfo.ArenaIsolation == expectedIsolation &&
		runtimeInfo.LocalExecutionReady
}

func prepareArenaOutputSchema(
	taskKind string,
) (string, string, string, func(), error) {
	claudeSchema, err := arenaActionOutputSchema(taskKind)
	if err != nil {
		return "", "", "", nil, err
	}
	codexSchema, err := arenaActionCodexOutputSchema(taskKind)
	if err != nil {
		return "", "", "", nil, err
	}
	directory, err := os.MkdirTemp("", "arena402-task-*")
	if err != nil {
		return "", "", "", nil, fmt.Errorf("create Arena task directory: %w", err)
	}
	cleanup := func() {
		_ = os.RemoveAll(directory)
	}
	path := filepath.Join(directory, "action-schema.json")
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		cleanup()
		return "", "", "", nil, fmt.Errorf("create Arena output schema: %w", err)
	}
	if _, err := file.Write(codexSchema); err != nil {
		_ = file.Close()
		cleanup()
		return "", "", "", nil, fmt.Errorf("write Arena output schema: %w", err)
	}
	if err := file.Close(); err != nil {
		cleanup()
		return "", "", "", nil, fmt.Errorf("close Arena output schema: %w", err)
	}
	return string(claudeSchema), path, directory, cleanup, nil
}

func (s *Supervisor) emit(event protocol.RuntimeEvent) {
	if event.BindingID == "" && event.SessionID != "" {
		s.mu.RLock()
		if current, found := s.sessions[event.SessionID]; found {
			event.BindingID = current.BindingID
		}
		s.mu.RUnlock()
	}
	s.events <- event
}

func stopTask(running *task) {
	if running == nil {
		return
	}
	running.cancelled.Store(true)
	terminateProcess(running.command, running.containment)
	running.cancel()
}

func normalizeRoots(roots []string) ([]string, error) {
	if len(roots) == 0 {
		current, err := os.Getwd()
		if err != nil {
			return nil, err
		}
		roots = []string{current}
	}
	normalized := make([]string, 0, len(roots))
	for _, root := range roots {
		resolved, err := resolvePath(root)
		if err != nil {
			return nil, fmt.Errorf("resolve allowed root %q: %w", root, err)
		}
		info, err := os.Stat(resolved)
		if err != nil || !info.IsDir() {
			return nil, fmt.Errorf("allowed root %q is not a readable directory", root)
		}
		normalized = append(normalized, resolved)
	}
	return normalized, nil
}

func resolvePath(path string) (string, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", err
	}
	return filepath.Clean(resolved), nil
}

func pathWithin(root, target string) bool {
	if runtime.GOOS == "windows" {
		root = strings.ToLower(root)
		target = strings.ToLower(target)
	}
	relative, err := filepath.Rel(root, target)
	if err != nil || filepath.IsAbs(relative) {
		return false
	}
	return relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func copyInventory(inventory protocol.InventorySnapshot) protocol.InventorySnapshot {
	copyOfInventory := inventory
	copyOfInventory.Runtimes = append([]protocol.Runtime(nil), inventory.Runtimes...)
	return copyOfInventory
}

func buildEnvironment(refs []string, allowed map[string]struct{}) ([]string, error) {
	baseNames := []string{
		"PATH",
		"HOME",
		"USERPROFILE",
		"LOCALAPPDATA",
		"APPDATA",
		"CODEX_HOME",
		"TMP",
		"TEMP",
		"TMPDIR",
		"SystemRoot",
		"COMSPEC",
		"LANG",
		"LC_ALL",
		"TERM",
	}
	seen := make(map[string]struct{})
	environment := make([]string, 0, len(baseNames)+len(refs))
	appendName := func(name string) {
		key := name
		if runtime.GOOS == "windows" {
			key = strings.ToLower(name)
		}
		if _, exists := seen[key]; exists {
			return
		}
		if value, exists := os.LookupEnv(name); exists {
			environment = append(environment, name+"="+value)
			seen[key] = struct{}{}
		}
	}
	for _, name := range baseNames {
		appendName(name)
	}
	for _, name := range refs {
		if !environmentNamePattern.MatchString(name) {
			return nil, fmt.Errorf("environment reference %q is not a variable name", name)
		}
		allowedKey := name
		if runtime.GOOS == "windows" {
			allowedKey = strings.ToLower(name)
		}
		if _, permitted := allowed[allowedKey]; !permitted {
			return nil, fmt.Errorf("environment reference %q is not locally allowed", name)
		}
		if _, exists := os.LookupEnv(name); !exists {
			return nil, fmt.Errorf("environment reference %q is not configured locally", name)
		}
		appendName(name)
	}
	return environment, nil
}

func normalizeEnvironmentAllowlist(names []string) map[string]struct{} {
	allowed := make(map[string]struct{}, len(names))
	for _, name := range names {
		key := name
		if runtime.GOOS == "windows" {
			key = strings.ToLower(name)
		}
		allowed[key] = struct{}{}
	}
	return allowed
}

package protocol

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func validCommand(kind string) Command {
	return Command{
		CommandID:      "cmd-1",
		BindingID:      "binding-1",
		AgentID:        "agent-1",
		Kind:           kind,
		IdempotencyKey: "idem-1",
		RuntimeID:      "runtime-1",
		SessionID:      "session-1",
		BindingEpoch:   1,
		ExpiresAt:      time.Now().Add(time.Minute),
		Payload:        json.RawMessage(`{}`),
	}
}

func TestCommandValidateAllowsOnlyTypedCommands(t *testing.T) {
	for _, kind := range []string{
		CommandRuntimeProbe,
		CommandSessionStart,
		CommandTaskDispatch,
		CommandTaskCancel,
		CommandSessionStop,
		CommandSessionResume,
	} {
		command := validCommand(kind)
		if kind == CommandRuntimeProbe {
			command.AgentID = ""
			command.SessionID = ""
			command.RuntimeID = ""
			command.BindingEpoch = 0
		}
		if err := command.Validate(time.Now()); err != nil {
			t.Fatalf("%s should be valid: %v", kind, err)
		}
	}

	command := validCommand("shell.exec")
	err := command.Validate(time.Now())
	if err == nil || !strings.Contains(err.Error(), "unsupported command") {
		t.Fatalf("arbitrary command should be rejected, got %v", err)
	}
}

func TestCommandValidateRejectsExpiredAndUnboundCommands(t *testing.T) {
	expired := validCommand(CommandTaskDispatch)
	expired.ExpiresAt = time.Now().Add(-time.Second)
	if err := expired.Validate(time.Now()); err == nil {
		t.Fatal("expired command should be rejected")
	}

	unbound := validCommand(CommandTaskDispatch)
	unbound.BindingEpoch = 0
	if err := unbound.Validate(time.Now()); err == nil {
		t.Fatal("command without binding epoch should be rejected")
	}

	for field, mutate := range map[string]func(*Command){
		"agent_id":   func(command *Command) { command.AgentID = "" },
		"runtime_id": func(command *Command) { command.RuntimeID = "" },
	} {
		command := validCommand(CommandSessionStop)
		mutate(&command)
		if err := command.Validate(time.Now()); err == nil || !strings.Contains(err.Error(), field) {
			t.Fatalf("command without %s should be rejected, got %v", field, err)
		}
	}
}

package driver

import (
	"context"
	"reflect"
	"testing"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
)

func TestClaudeDriverBuildsFixedNonShellCommand(t *testing.T) {
	runtimeInfo := protocol.Runtime{Kind: "claude_code", ExecutablePath: "/opt/bin/claude"}
	session := SessionSpec{SessionID: "session-1", WorkingDir: "/workspace", ResumeToken: "resume-1"}
	task := TaskSpec{TaskID: "task-1", Prompt: "hello; rm -rf /"}

	command, err := (ClaudeDriver{}).BuildTask(context.Background(), runtimeInfo, session, task)
	if err != nil {
		t.Fatal(err)
	}
	expected := []string{
		"/opt/bin/claude",
		"--print",
		"--output-format",
		"stream-json",
		"--verbose",
		"--resume",
		"resume-1",
	}
	if !reflect.DeepEqual(command.Args, expected) {
		t.Fatalf("unexpected args: %#v", command.Args)
	}
	for _, arg := range command.Args {
		if arg == task.Prompt {
			t.Fatal("prompt must be provided over stdin, not interpreted as command arguments")
		}
	}
}

func TestCodexDriverBuildsFixedResumeCommand(t *testing.T) {
	runtimeInfo := protocol.Runtime{Kind: "codex", ExecutablePath: "/opt/bin/codex"}
	session := SessionSpec{SessionID: "session-1", WorkingDir: "/workspace", ResumeToken: "thread-1"}
	task := TaskSpec{TaskID: "task-1", Prompt: "continue"}

	command, err := (CodexDriver{}).BuildTask(context.Background(), runtimeInfo, session, task)
	if err != nil {
		t.Fatal(err)
	}
	expected := []string{"/opt/bin/codex", "exec", "--json", "resume", "thread-1", "-"}
	if !reflect.DeepEqual(command.Args, expected) {
		t.Fatalf("unexpected args: %#v", command.Args)
	}
}

func TestCodexArenaTaskUsesRestrictedEphemeralProfile(t *testing.T) {
	runtimeInfo := protocol.Runtime{Kind: "codex", ExecutablePath: "/opt/bin/codex"}
	session := SessionSpec{SessionID: "session-1", WorkingDir: "/workspace"}
	task := TaskSpec{
		TaskID:             "task-1",
		Prompt:             `{"kind":"arena.decide"}`,
		ArenaKind:          "arena.decide",
		OutputSchemaPath:   "/tmp/arena-action-schema.json",
		IsolatedWorkingDir: "/tmp/arena-task",
	}

	command, err := (CodexDriver{}).BuildTask(context.Background(), runtimeInfo, session, task)
	if err != nil {
		t.Fatal(err)
	}
	expected := []string{
		"/opt/bin/codex",
		"exec",
		"--json",
		"--sandbox",
		"read-only",
		"--ephemeral",
		"--ignore-user-config",
		"--ignore-rules",
		"--cd",
		"/tmp/arena-task",
		"--output-schema",
		"/tmp/arena-action-schema.json",
		"-",
	}
	if !reflect.DeepEqual(command.Args, expected) {
		t.Fatalf("unexpected args: %#v", command.Args)
	}
	if command.Dir != "/tmp/arena-task" {
		t.Fatalf("Arena task must not run in the user project: %s", command.Dir)
	}
}

func TestClaudeArenaTaskDisablesToolsCustomizationsAndPersistence(t *testing.T) {
	runtimeInfo := protocol.Runtime{Kind: "claude_code", ExecutablePath: "/opt/bin/claude"}
	session := SessionSpec{
		SessionID:   "session-1",
		WorkingDir:  "/workspace",
		ResumeToken: "must-not-be-used",
	}
	task := TaskSpec{
		TaskID:             "task-1",
		Prompt:             `{"kind":"arena.negotiate"}`,
		ArenaKind:          "arena.negotiate",
		OutputSchema:       `{"type":"object"}`,
		IsolatedWorkingDir: "/tmp/arena-task",
	}

	command, err := (ClaudeDriver{}).BuildTask(context.Background(), runtimeInfo, session, task)
	if err != nil {
		t.Fatal(err)
	}
	expected := []string{
		"/opt/bin/claude",
		"--print",
		"--output-format",
		"stream-json",
		"--verbose",
		"--safe-mode",
		"--strict-mcp-config",
		"--mcp-config",
		`{"mcpServers":{}}`,
		"--tools",
		"",
		"--disable-slash-commands",
		"--no-session-persistence",
		"--permission-mode",
		"dontAsk",
		"--json-schema",
		`{"type":"object"}`,
	}
	if !reflect.DeepEqual(command.Args, expected) {
		t.Fatalf("unexpected args: %#v", command.Args)
	}
	if command.Dir != "/tmp/arena-task" {
		t.Fatalf("Arena task must not run in the user project: %s", command.Dir)
	}
}

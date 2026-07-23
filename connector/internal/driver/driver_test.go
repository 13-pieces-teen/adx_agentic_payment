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

package driver

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
)

type SessionSpec struct {
	SessionID   string
	WorkingDir  string
	ResumeToken string
	Environment []string
}

type TaskSpec struct {
	TaskID             string
	Prompt             string
	ArenaKind          string
	OutputSchema       string
	OutputSchemaPath   string
	IsolatedWorkingDir string
}

type Driver interface {
	Kind() string
	BuildTask(context.Context, protocol.Runtime, SessionSpec, TaskSpec) (*exec.Cmd, error)
}

type Registry struct {
	drivers map[string]Driver
}

func NewRegistry(drivers ...Driver) *Registry {
	registry := &Registry{drivers: make(map[string]Driver)}
	for _, runtimeDriver := range drivers {
		registry.drivers[runtimeDriver.Kind()] = runtimeDriver
	}
	return registry
}

func DefaultRegistry() *Registry {
	return NewRegistry(ClaudeDriver{}, CodexDriver{})
}

func (r *Registry) Driver(kind string) (Driver, bool) {
	runtimeDriver, ok := r.drivers[kind]
	return runtimeDriver, ok
}

type ClaudeDriver struct{}

func (ClaudeDriver) Kind() string {
	return "claude_code"
}

func (ClaudeDriver) BuildTask(
	ctx context.Context,
	runtimeInfo protocol.Runtime,
	session SessionSpec,
	task TaskSpec,
) (*exec.Cmd, error) {
	if err := validate(runtimeInfo, session, task); err != nil {
		return nil, err
	}
	args := []string{
		"--print",
		"--output-format", "stream-json",
		"--verbose",
	}
	workingDirectory := session.WorkingDir
	if task.ArenaKind != "" {
		if task.OutputSchema == "" || task.IsolatedWorkingDir == "" {
			return nil, errors.New("Arena output schema and isolated working directory are required")
		}
		workingDirectory = task.IsolatedWorkingDir
		args = append(
			args,
			"--safe-mode",
			"--strict-mcp-config",
			"--mcp-config", `{"mcpServers":{}}`,
			"--tools", "",
			"--disable-slash-commands",
			"--no-session-persistence",
			"--permission-mode", "dontAsk",
			"--json-schema", task.OutputSchema,
		)
	} else if session.ResumeToken != "" {
		args = append(args, "--resume", session.ResumeToken)
	}
	command := exec.CommandContext(ctx, runtimeInfo.ExecutablePath, args...)
	command.Dir = workingDirectory
	command.Stdin = strings.NewReader(task.Prompt)
	command.Env = append([]string(nil), session.Environment...)
	return command, nil
}

type CodexDriver struct{}

func (CodexDriver) Kind() string {
	return "codex"
}

func (CodexDriver) BuildTask(
	ctx context.Context,
	runtimeInfo protocol.Runtime,
	session SessionSpec,
	task TaskSpec,
) (*exec.Cmd, error) {
	if err := validate(runtimeInfo, session, task); err != nil {
		return nil, err
	}
	args := []string{"exec", "--json"}
	workingDirectory := session.WorkingDir
	if task.ArenaKind != "" {
		if task.OutputSchemaPath == "" || task.IsolatedWorkingDir == "" {
			return nil, errors.New("Arena output schema and isolated working directory are required")
		}
		workingDirectory = task.IsolatedWorkingDir
		args = append(
			args,
			"--sandbox", "read-only",
			"--ephemeral",
			"--ignore-user-config",
			"--ignore-rules",
			"--skip-git-repo-check",
			"--cd", task.IsolatedWorkingDir,
			"--output-schema", task.OutputSchemaPath,
		)
	} else if session.ResumeToken != "" {
		args = append(args, "resume", session.ResumeToken)
	}
	args = append(args, "-")
	command := exec.CommandContext(ctx, runtimeInfo.ExecutablePath, args...)
	command.Dir = workingDirectory
	command.Stdin = strings.NewReader(task.Prompt)
	command.Env = append([]string(nil), session.Environment...)
	return command, nil
}

func validate(runtimeInfo protocol.Runtime, session SessionSpec, task TaskSpec) error {
	if runtimeInfo.ExecutablePath == "" {
		return errors.New("runtime executable path is required")
	}
	if session.WorkingDir == "" {
		return errors.New("session working directory is required")
	}
	if task.TaskID == "" {
		return errors.New("task_id is required")
	}
	if strings.TrimSpace(task.Prompt) == "" {
		return errors.New("task prompt is required")
	}
	if len(task.Prompt) > 1_000_000 {
		return fmt.Errorf("task prompt exceeds 1000000 bytes")
	}
	return nil
}

package discovery

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestScannerIsDetectionOnlyUntilRuntimeTasksAreExplicitlyEnabled(t *testing.T) {
	paths := map[string]string{
		"claude": filepath.Join(t.TempDir(), "claude"),
		"codex":  filepath.Join(t.TempDir(), "codex"),
	}
	scanner := NewScanner("test", time.Second)
	scanner.LookPath = func(command string) (string, error) {
		path, ok := paths[command]
		if !ok {
			return "", errors.New("not found")
		}
		return path, nil
	}
	scanner.ReadVersion = func(_ context.Context, path string) (string, error) {
		return filepath.Base(path) + " 1.2.3", nil
	}
	scanner.Hostname = func() (string, error) { return "test-host", nil }

	inventory := scanner.Scan(context.Background())
	if len(inventory.Runtimes) != 2 {
		t.Fatalf("expected two runtimes, got %d", len(inventory.Runtimes))
	}
	for _, runtimeInfo := range inventory.Runtimes {
		if runtimeInfo.ID == "" || runtimeInfo.Status != "ready" {
			t.Fatalf("unexpected runtime: %#v", runtimeInfo)
		}
		if len(runtimeInfo.Capabilities) != 1 || runtimeInfo.Capabilities[0] != "runtime.probe" {
			t.Fatalf("%s should be detection-only by default: %#v", runtimeInfo.Kind, runtimeInfo.Capabilities)
		}
	}

	scanner.EnableTaskExecution("codex")
	inventory = scanner.Scan(context.Background())
	for _, runtimeInfo := range inventory.Runtimes {
		hasDispatch := false
		for _, capability := range runtimeInfo.Capabilities {
			if capability == "task.dispatch" {
				hasDispatch = true
			}
		}
		if runtimeInfo.Kind == "codex" && !hasDispatch {
			t.Fatalf("explicitly enabled Codex should advertise task.dispatch")
		}
		if runtimeInfo.Kind == "claude_code" && hasDispatch {
			t.Fatalf("Claude should remain detection-only")
		}
	}
}

func TestScannerKeepsExecutableWhenVersionProbeFails(t *testing.T) {
	scanner := NewScanner("test", time.Second)
	scanner.Candidates = scanner.Candidates[:1]
	scanner.LookPath = func(string) (string, error) { return "/tmp/claude", nil }
	scanner.ReadVersion = func(context.Context, string) (string, error) {
		return "", errors.New("probe timeout")
	}
	scanner.Hostname = func() (string, error) { return "test-host", nil }

	inventory := scanner.Scan(context.Background())
	if len(inventory.Runtimes) != 1 {
		t.Fatalf("expected degraded runtime, got %d", len(inventory.Runtimes))
	}
	if inventory.Runtimes[0].Status != "degraded" {
		t.Fatalf("expected degraded status, got %s", inventory.Runtimes[0].Status)
	}
}

func TestScannerReportsLocalExecutionReadinessSeparatelyFromDetection(t *testing.T) {
	paths := map[string]string{
		"claude": filepath.Join(t.TempDir(), "claude"),
		"codex":  filepath.Join(t.TempDir(), "codex"),
	}
	scanner := NewScanner("test", time.Second)
	scanner.LookPath = func(command string) (string, error) {
		return paths[command], nil
	}
	scanner.ReadVersion = func(context.Context, string) (string, error) {
		return "1.2.3", nil
	}
	scanner.ReadAuthStatus = func(_ context.Context, _ string, kind string) error {
		if kind == "codex" {
			return nil
		}
		return errors.New("not authenticated")
	}
	scanner.ReadArenaCompatibility = func(context.Context, string, string) error {
		return nil
	}
	scanner.Hostname = func() (string, error) { return "test-host", nil }
	scanner.EnableTaskExecution("codex")

	inventory := scanner.Scan(context.Background())
	for _, runtimeInfo := range inventory.Runtimes {
		switch runtimeInfo.Kind {
		case "codex":
			if !runtimeInfo.TaskEnabled ||
				runtimeInfo.AuthenticationStatus != "configured" ||
				!runtimeInfo.ArenaCompatible ||
				runtimeInfo.ArenaIsolation != "read_only_ephemeral_schema" ||
				!runtimeInfo.LocalExecutionReady {
				t.Fatalf("Codex readiness was not reported accurately: %#v", runtimeInfo)
			}
		case "claude_code":
			if runtimeInfo.TaskEnabled ||
				runtimeInfo.AuthenticationStatus != "unavailable" ||
				!runtimeInfo.ArenaCompatible ||
				runtimeInfo.ArenaIsolation != "none" ||
				runtimeInfo.LocalExecutionReady {
				t.Fatalf("Claude detection must not imply task readiness: %#v", runtimeInfo)
			}
		}
	}
}

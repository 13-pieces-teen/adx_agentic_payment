//go:build !windows

package store

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
)

func TestFileStoreDoesNotChangeExistingParentDirectoryMode(t *testing.T) {
	root := t.TempDir()
	parent := filepath.Join(root, "shared")
	if err := os.Mkdir(parent, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(parent, 0o755); err != nil {
		t.Fatal(err)
	}

	statePath := filepath.Join(parent, "custom-state.json")
	if err := NewFileStore(statePath).SaveCredentials(testCredentials()); err != nil {
		t.Fatal(err)
	}

	assertPermission(t, parent, 0o755)
	assertPermission(t, statePath, 0o600)
}

func TestFileOutboxDoesNotChangeExistingDirectoryMode(t *testing.T) {
	root := t.TempDir()
	parent := filepath.Join(root, "shared")
	outboxDirectory := filepath.Join(parent, "outbox")
	if err := os.MkdirAll(outboxDirectory, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(parent, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(outboxDirectory, 0o755); err != nil {
		t.Fatal(err)
	}

	event := protocol.NewRuntimeEvent("runtime-1", "session-1", "task-1", "runtime.stdout", nil)
	event.Sequence = 1
	if err := NewFileOutbox(filepath.Join(parent, "state.json")).Append(event); err != nil {
		t.Fatal(err)
	}

	assertPermission(t, parent, 0o755)
	assertPermission(t, outboxDirectory, 0o755)
	assertPermission(t, filepath.Join(outboxDirectory, eventFilename(1)), 0o600)
}

func TestConnectorHardensOnlyDirectoriesItCreates(t *testing.T) {
	root := t.TempDir()
	existingParent := filepath.Join(root, "shared")
	createdDirectory := filepath.Join(existingParent, "adx", "connector")
	if err := os.Mkdir(existingParent, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(existingParent, 0o755); err != nil {
		t.Fatal(err)
	}

	statePath := filepath.Join(createdDirectory, "state.json")
	if err := NewFileStore(statePath).SaveCredentials(testCredentials()); err != nil {
		t.Fatal(err)
	}

	assertPermission(t, existingParent, 0o755)
	assertPermission(t, filepath.Join(existingParent, "adx"), 0o700)
	assertPermission(t, createdDirectory, 0o700)
}

func TestStateLockDoesNotChangeExistingParentDirectoryMode(t *testing.T) {
	root := t.TempDir()
	parent := filepath.Join(root, "shared")
	if err := os.Mkdir(parent, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(parent, 0o755); err != nil {
		t.Fatal(err)
	}

	lock, err := AcquireStateLock(filepath.Join(parent, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := lock.Close(); err != nil {
		t.Fatal(err)
	}

	assertPermission(t, parent, 0o755)
	assertPermission(t, filepath.Join(parent, "state.json.lock"), 0o600)
}

func testCredentials() Credentials {
	return Credentials{
		DeviceID:   "device-1",
		Token:      "secret-device-token",
		GatewayURL: "wss://example.test/connectors/ws",
	}
}

func assertPermission(t *testing.T, path string, want os.FileMode) {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != want {
		t.Fatalf("%s mode = %04o, want %04o", path, got, want)
	}
}

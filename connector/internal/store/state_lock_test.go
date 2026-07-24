package store

import (
	"errors"
	"path/filepath"
	"testing"
)

func TestStateLockIsExclusiveAndReusableAfterClose(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "state.json")
	first, err := AcquireStateLock(statePath)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()

	second, err := AcquireStateLock(statePath)
	if !errors.Is(err, ErrStateLocked) {
		if second != nil {
			_ = second.Close()
		}
		t.Fatalf("second state lock error = %v, want ErrStateLocked", err)
	}

	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	reused, err := AcquireStateLock(statePath)
	if err != nil {
		t.Fatalf("state lock was not reusable after close: %v", err)
	}
	if err := reused.Close(); err != nil {
		t.Fatal(err)
	}
}

//go:build windows

package store

import (
	"bufio"
	"bytes"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

const (
	stateLockHelperEnv  = "ADX_STATE_LOCK_TEST_HELPER"
	stateLockPathEnv    = "ADX_STATE_LOCK_TEST_PATH"
	stateLockReadyToken = "state-lock-ready"
)

func TestStateLockIsReleasedWhenHoldingProcessCrashes(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "state.json")
	command := exec.Command(os.Args[0], "-test.run=^TestStateLockCrashHelper$")
	command.Env = append(
		os.Environ(),
		stateLockHelperEnv+"=1",
		stateLockPathEnv+"="+statePath,
	)
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	stopped := false
	defer func() {
		if !stopped {
			_ = command.Process.Kill()
			_ = command.Wait()
		}
	}()

	ready := make(chan string, 1)
	go func() {
		scanner := bufio.NewScanner(stdout)
		if scanner.Scan() {
			ready <- scanner.Text()
			return
		}
		ready <- ""
	}()
	select {
	case line := <-ready:
		if strings.TrimSpace(line) != stateLockReadyToken {
			t.Fatalf("lock helper did not become ready; stdout=%q stderr=%q", line, stderr.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatalf("timed out waiting for lock helper; stderr=%q", stderr.String())
	}

	contended, err := AcquireStateLock(statePath)
	if !errors.Is(err, ErrStateLocked) {
		if contended != nil {
			_ = contended.Close()
		}
		t.Fatalf("state lock held by helper returned %v, want ErrStateLocked", err)
	}

	if err := command.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = command.Wait()
	stopped = true

	deadline := time.Now().Add(5 * time.Second)
	for {
		recovered, lockErr := AcquireStateLock(statePath)
		if lockErr == nil {
			if err := recovered.Close(); err != nil {
				t.Fatal(err)
			}
			return
		}
		if !errors.Is(lockErr, ErrStateLocked) || time.Now().After(deadline) {
			t.Fatalf("state lock was not released after helper crash: %v", lockErr)
		}
		time.Sleep(25 * time.Millisecond)
	}
}

func TestStateLockCrashHelper(t *testing.T) {
	if os.Getenv(stateLockHelperEnv) != "1" {
		return
	}
	lock, err := AcquireStateLock(os.Getenv(stateLockPathEnv))
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if _, err := os.Stdout.WriteString(stateLockReadyToken + "\n"); err != nil {
		t.Fatal(err)
	}
	time.Sleep(10 * time.Minute)
}

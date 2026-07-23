package store

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

var ErrStateLocked = errors.New("connector state is already in use by another process")

type StateLock struct {
	mu     sync.Mutex
	file   *os.File
	unlock func(*os.File) error
}

func prepareStateLockFile(statePath string) (*os.File, string, error) {
	if strings.TrimSpace(statePath) == "" {
		return nil, "", errors.New("connector state path is required")
	}
	lockPath := statePath + ".lock"
	if err := ensureConnectorDirectory(filepath.Dir(lockPath)); err != nil {
		return nil, "", fmt.Errorf("create connector state lock directory: %w", err)
	}
	file, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, "", fmt.Errorf("open connector state lock %s: %w", lockPath, err)
	}
	if err := secureOpenFile(file, lockPath); err != nil {
		_ = file.Close()
		return nil, "", fmt.Errorf("protect connector state lock %s: %w", lockPath, err)
	}
	return file, lockPath, nil
}

func newStateLock(file *os.File, unlock func(*os.File) error) *StateLock {
	return &StateLock{file: file, unlock: unlock}
}

func stateLockedError(lockPath string) error {
	return fmt.Errorf("%w: %s", ErrStateLocked, lockPath)
}

func (lock *StateLock) Close() error {
	if lock == nil {
		return nil
	}
	lock.mu.Lock()
	defer lock.mu.Unlock()
	if lock.file == nil {
		return nil
	}
	file := lock.file
	lock.file = nil
	unlockErr := lock.unlock(file)
	closeErr := file.Close()
	return errors.Join(unlockErr, closeErr)
}

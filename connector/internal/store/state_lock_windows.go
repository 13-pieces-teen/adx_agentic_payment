//go:build windows

package store

import (
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/windows"
)

func AcquireStateLock(statePath string) (*StateLock, error) {
	file, lockPath, err := prepareStateLockFile(statePath)
	if err != nil {
		return nil, err
	}
	overlapped := windows.Overlapped{}
	err = windows.LockFileEx(
		windows.Handle(file.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
		0,
		1,
		0,
		&overlapped,
	)
	if err != nil {
		_ = file.Close()
		if errors.Is(err, windows.ERROR_LOCK_VIOLATION) {
			return nil, stateLockedError(lockPath)
		}
		return nil, fmt.Errorf("lock connector state %s: %w", lockPath, err)
	}
	return newStateLock(file, func(locked *os.File) error {
		return windows.UnlockFileEx(
			windows.Handle(locked.Fd()),
			0,
			1,
			0,
			&windows.Overlapped{},
		)
	}), nil
}

//go:build aix

package store

import (
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

func AcquireStateLock(statePath string) (*StateLock, error) {
	file, lockPath, err := prepareStateLockFile(statePath)
	if err != nil {
		return nil, err
	}
	writeLock := unix.Flock_t{Type: unix.F_WRLCK}
	err = unix.FcntlFlock(file.Fd(), unix.F_SETLK, &writeLock)
	if err != nil {
		_ = file.Close()
		if errors.Is(err, unix.EACCES) || errors.Is(err, unix.EAGAIN) {
			return nil, stateLockedError(lockPath)
		}
		return nil, fmt.Errorf("lock connector state %s: %w", lockPath, err)
	}
	return newStateLock(file, func(locked *os.File) error {
		unlock := unix.Flock_t{Type: unix.F_UNLCK}
		return unix.FcntlFlock(locked.Fd(), unix.F_SETLK, &unlock)
	}), nil
}

//go:build darwin || dragonfly || freebsd || linux || netbsd || openbsd || solaris

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
	err = unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB)
	if err != nil {
		_ = file.Close()
		if errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN) {
			return nil, stateLockedError(lockPath)
		}
		return nil, fmt.Errorf("lock connector state %s: %w", lockPath, err)
	}
	return newStateLock(file, func(locked *os.File) error {
		return unix.Flock(int(locked.Fd()), unix.LOCK_UN)
	}), nil
}

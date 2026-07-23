//go:build !windows && !linux

package supervisor

import (
	"os/exec"
	"syscall"
)

type processContainment struct{}

func configureProcess(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func startManagedProcess(command *exec.Cmd) (*processContainment, error) {
	configureProcess(command)
	if err := command.Start(); err != nil {
		return nil, err
	}
	return &processContainment{}, nil
}

func releaseProcess(_ *processContainment) {}

func terminateProcess(command *exec.Cmd, _ *processContainment) {
	if command == nil || command.Process == nil {
		return
	}
	if err := syscall.Kill(-command.Process.Pid, syscall.SIGKILL); err != nil {
		_ = command.Process.Kill()
	}
}

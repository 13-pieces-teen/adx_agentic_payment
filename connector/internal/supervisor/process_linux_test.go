//go:build linux

package supervisor

import (
	"os/exec"
	"syscall"
	"testing"
)

func TestConfigureProcessContainsLinuxChild(t *testing.T) {
	command := &exec.Cmd{}
	configureProcess(command)
	if command.SysProcAttr == nil {
		t.Fatal("linux child process attributes were not configured")
	}
	if !command.SysProcAttr.Setpgid {
		t.Fatal("linux child must run in a separate process group")
	}
	if command.SysProcAttr.Pdeathsig != syscall.SIGKILL {
		t.Fatalf("linux child parent-death signal = %v, want SIGKILL", command.SysProcAttr.Pdeathsig)
	}
}

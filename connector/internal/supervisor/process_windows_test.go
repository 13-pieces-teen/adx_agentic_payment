//go:build windows

package supervisor

import (
	"os/exec"
	"syscall"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

func TestConfigureProcessStartsWindowsChildSuspended(t *testing.T) {
	command := &exec.Cmd{}
	configureProcess(command)
	if command.SysProcAttr == nil {
		t.Fatal("windows child process attributes were not configured")
	}
	flags := command.SysProcAttr.CreationFlags
	if flags&windows.CREATE_SUSPENDED == 0 {
		t.Fatal("windows child must start suspended until Job Object assignment succeeds")
	}
	if flags&syscall.CREATE_NEW_PROCESS_GROUP == 0 {
		t.Fatal("windows child must run in a separate process group")
	}
}

func TestNewKillOnCloseJobConfiguresLimit(t *testing.T) {
	containment, err := newKillOnCloseJob()
	if err != nil {
		t.Fatalf("newKillOnCloseJob() error = %v", err)
	}
	defer releaseProcess(containment)

	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	if err := windows.QueryInformationJobObject(
		containment.job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)),
		uint32(unsafe.Sizeof(info)),
		nil,
	); err != nil {
		t.Fatalf("QueryInformationJobObject() error = %v", err)
	}
	if info.BasicLimitInformation.LimitFlags&windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0 {
		t.Fatal("Job Object does not have JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE")
	}
}

func TestManagedWindowsProcessIsResumedAndCompletes(t *testing.T) {
	command := exec.Command("cmd.exe", "/d", "/s", "/c", "exit 0")
	containment, err := startManagedProcess(command)
	if err != nil {
		t.Fatalf("startManagedProcess() error = %v", err)
	}
	defer releaseProcess(containment)

	wait := make(chan error, 1)
	go func() {
		wait <- command.Wait()
	}()
	select {
	case err := <-wait:
		if err != nil {
			t.Fatalf("managed process Wait() error = %v", err)
		}
	case <-time.After(5 * time.Second):
		terminateProcess(command, containment)
		t.Fatal("managed process remained suspended")
	}
}

func TestTerminateProcessEndsWindowsJob(t *testing.T) {
	command := exec.Command("cmd.exe", "/d", "/s", "/c", "ping.exe 127.0.0.1 -n 30 >NUL")
	containment, err := startManagedProcess(command)
	if err != nil {
		t.Fatalf("startManagedProcess() error = %v", err)
	}

	wait := make(chan error, 1)
	go func() {
		wait <- command.Wait()
	}()
	terminateProcess(command, containment)
	select {
	case <-wait:
	case <-time.After(5 * time.Second):
		_ = command.Process.Kill()
		t.Fatal("terminating the Job Object did not end the managed process")
	}
}

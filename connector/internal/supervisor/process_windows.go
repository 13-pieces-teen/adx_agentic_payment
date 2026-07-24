//go:build windows

package supervisor

import (
	"errors"
	"fmt"
	"os/exec"
	"strconv"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

type processContainment struct {
	mu  sync.Mutex
	job windows.Handle
}

func configureProcess(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP | windows.CREATE_SUSPENDED,
	}
}

func startManagedProcess(command *exec.Cmd) (*processContainment, error) {
	containment, err := newKillOnCloseJob()
	if err != nil {
		return nil, fmt.Errorf("create KILL_ON_JOB_CLOSE Job Object: %w", err)
	}

	configureProcess(command)
	if err := command.Start(); err != nil {
		_ = containment.close()
		return nil, err
	}

	if err := containment.assign(command.Process.Pid); err != nil {
		abortStartedProcess(command, containment)
		return nil, fmt.Errorf(
			"assign newly started process %d to KILL_ON_JOB_CLOSE Job Object: %w",
			command.Process.Pid,
			err,
		)
	}
	if err := resumePrimaryThread(uint32(command.Process.Pid)); err != nil {
		abortStartedProcess(command, containment)
		return nil, fmt.Errorf(
			"resume Job-contained process %d: %w",
			command.Process.Pid,
			err,
		)
	}
	return containment, nil
}

func newKillOnCloseJob() (*processContainment, error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return nil, err
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(
		job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)),
		uint32(unsafe.Sizeof(info)),
	); err != nil {
		_ = windows.CloseHandle(job)
		return nil, err
	}
	return &processContainment{job: job}, nil
}

func (containment *processContainment) assign(pid int) error {
	process, err := windows.OpenProcess(
		windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE,
		false,
		uint32(pid),
	)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(process)
	return windows.AssignProcessToJobObject(containment.job, process)
}

func resumePrimaryThread(pid uint32) error {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPTHREAD, 0)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(snapshot)

	entry := windows.ThreadEntry32{Size: uint32(unsafe.Sizeof(windows.ThreadEntry32{}))}
	for err = windows.Thread32First(snapshot, &entry); err == nil; err = windows.Thread32Next(snapshot, &entry) {
		if entry.OwnerProcessID != pid {
			continue
		}
		thread, openErr := windows.OpenThread(windows.THREAD_SUSPEND_RESUME, false, entry.ThreadID)
		if openErr != nil {
			return openErr
		}
		_, resumeErr := windows.ResumeThread(thread)
		closeErr := windows.CloseHandle(thread)
		if resumeErr != nil {
			return resumeErr
		}
		if closeErr != nil {
			return closeErr
		}
		return nil
	}
	if err != nil && !errors.Is(err, windows.ERROR_NO_MORE_FILES) {
		return err
	}
	return errors.New("primary thread was not found")
}

func abortStartedProcess(command *exec.Cmd, containment *processContainment) {
	if containment != nil {
		_ = containment.terminate()
	}
	if command != nil && command.Process != nil {
		_ = command.Process.Kill()
		_ = command.Wait()
	}
}

func releaseProcess(containment *processContainment) {
	if containment != nil {
		_ = containment.close()
	}
}

func terminateProcess(command *exec.Cmd, containment *processContainment) {
	if command == nil || command.Process == nil {
		releaseProcess(containment)
		return
	}
	if containment != nil && containment.terminate() == nil {
		return
	}
	killTree := exec.Command(
		"taskkill.exe",
		"/PID", strconv.Itoa(command.Process.Pid),
		"/T",
		"/F",
	)
	if err := killTree.Run(); err != nil {
		_ = command.Process.Kill()
	}
	releaseProcess(containment)
}

func (containment *processContainment) terminate() error {
	containment.mu.Lock()
	defer containment.mu.Unlock()
	if containment.job == 0 {
		return nil
	}
	terminateErr := windows.TerminateJobObject(containment.job, 1)
	closeErr := windows.CloseHandle(containment.job)
	containment.job = 0
	if terminateErr != nil {
		return terminateErr
	}
	return closeErr
}

func (containment *processContainment) close() error {
	containment.mu.Lock()
	defer containment.mu.Unlock()
	if containment.job == 0 {
		return nil
	}
	err := windows.CloseHandle(containment.job)
	containment.job = 0
	return err
}

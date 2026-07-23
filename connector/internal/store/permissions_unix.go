//go:build !windows

package store

import "os"

func secureCreatedDirectory(path string) error {
	return os.Chmod(path, 0o700)
}

func secureFile(path string) error {
	return os.Chmod(path, 0o600)
}

func secureOpenFile(file *os.File, _ string) error {
	return file.Chmod(0o600)
}

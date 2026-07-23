package store

import (
	"fmt"
	"os"
	"path/filepath"
)

// ensureConnectorDirectory creates missing directories one component at a
// time. It hardens only components whose os.Mkdir call succeeded; an existing
// user-selected parent directory is deliberately left unchanged.
func ensureConnectorDirectory(path string) error {
	path = filepath.Clean(path)
	info, err := os.Stat(path)
	if err == nil {
		if !info.IsDir() {
			return fmt.Errorf("%s is not a directory", path)
		}
		return nil
	}
	if !os.IsNotExist(err) {
		return err
	}

	parent := filepath.Dir(path)
	if parent != path {
		if err := ensureConnectorDirectory(parent); err != nil {
			return err
		}
	}

	if err := os.Mkdir(path, 0o700); err != nil {
		if !os.IsExist(err) {
			return err
		}
		// Another process may have created the component after the Stat above.
		// Treat it as pre-existing and, critically, do not change its ACL/mode.
		info, statErr := os.Stat(path)
		if statErr != nil {
			return statErr
		}
		if !info.IsDir() {
			return fmt.Errorf("%s is not a directory", path)
		}
		return nil
	}
	return secureCreatedDirectory(path)
}

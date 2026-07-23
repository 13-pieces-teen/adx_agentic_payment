package main

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/adx-agentic-payment/adx/connector/internal/transport"
)

func TestSelectServerPrefersCanonicalFlagAndRejectsConflicts(t *testing.T) {
	selected, err := selectServer("https://arena.example/", "", "http://localhost:8000")
	if err != nil {
		t.Fatal(err)
	}
	if selected != "https://arena.example/" {
		t.Fatalf("selected server = %q", selected)
	}
	if _, err := selectServer(
		"https://one.example",
		"https://two.example",
		"",
	); err == nil {
		t.Fatal("conflicting server flags must be rejected")
	}
}

func TestAppendVersionedBinaryDirectoriesFindsOnlyVersionDirectories(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"v20.1.0", "v22.2.0"} {
		if err := os.MkdirAll(filepath.Join(root, name, "bin"), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(root, "README"), []byte("not a version"), 0o600); err != nil {
		t.Fatal(err)
	}

	paths := appendVersionedBinaryDirectories(nil, root, "bin")
	if len(paths) != 2 {
		t.Fatalf("versioned binary paths = %#v", paths)
	}
	for _, path := range paths {
		if filepath.Base(path) != "bin" {
			t.Fatalf("unexpected binary directory: %s", path)
		}
	}
}

func TestExitCodeStopsServiceRestartAfterRevocationOrReplacement(t *testing.T) {
	for _, err := range []error{
		transport.ErrDeviceRevoked,
		transport.ErrConnectionReplaced,
		errors.New("ordinary failure"),
	} {
		code := exitCode(err)
		if errors.Is(err, transport.ErrDeviceRevoked) ||
			errors.Is(err, transport.ErrConnectionReplaced) {
			if code != 78 {
				t.Fatalf("terminal device error exit code = %d", code)
			}
		} else if code != 1 {
			t.Fatalf("ordinary failure exit code = %d", code)
		}
	}
}

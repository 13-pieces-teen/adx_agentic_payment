package discovery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
)

type Candidate struct {
	Command      string
	Kind         string
	DisplayName  string
	Capabilities []string
	AuthModes    []string
}

type LookPathFunc func(string) (string, error)
type VersionFunc func(context.Context, string) (string, error)
type AuthStatusFunc func(context.Context, string, string) error
type CompatibilityFunc func(context.Context, string, string) error

type Scanner struct {
	Version                string
	Timeout                time.Duration
	Candidates             []Candidate
	LookPath               LookPathFunc
	ReadVersion            VersionFunc
	ReadAuthStatus         AuthStatusFunc
	ReadArenaCompatibility CompatibilityFunc
	Hostname               func() (string, error)
	AdditionalPaths        []string
}

var executionCapabilities = map[string][]string{
	"claude_code": {
		protocol.CommandRuntimeProbe,
		protocol.CommandSessionStart,
		protocol.CommandTaskDispatch,
		protocol.CommandTaskCancel,
		protocol.CommandSessionStop,
		protocol.CommandSessionResume,
		"events.stream_json",
	},
	"codex": {
		protocol.CommandRuntimeProbe,
		protocol.CommandSessionStart,
		protocol.CommandTaskDispatch,
		protocol.CommandTaskCancel,
		protocol.CommandSessionStop,
		protocol.CommandSessionResume,
		"events.jsonl",
	},
}

var arenaIsolationProfiles = map[string]string{
	"claude_code": "no_tools_safe_mode_schema",
	"codex":       "read_only_ephemeral_schema",
}

func NewScanner(connectorVersion string, timeout time.Duration) *Scanner {
	if timeout <= 0 {
		timeout = 3 * time.Second
	}
	return &Scanner{
		Version: connectorVersion,
		Timeout: timeout,
		Candidates: []Candidate{
			{
				Command:      "claude",
				Kind:         "claude_code",
				DisplayName:  "Claude Code",
				Capabilities: []string{protocol.CommandRuntimeProbe},
				AuthModes:    []string{"unverified_local_auth"},
			},
			{
				Command:      "codex",
				Kind:         "codex",
				DisplayName:  "OpenAI Codex",
				Capabilities: []string{protocol.CommandRuntimeProbe},
				AuthModes:    []string{"unverified_local_auth"},
			},
		},
		LookPath:               exec.LookPath,
		ReadVersion:            defaultReadVersion,
		ReadAuthStatus:         defaultReadAuthStatus,
		ReadArenaCompatibility: defaultReadArenaCompatibility,
		Hostname:               os.Hostname,
	}
}

// EnableTaskExecution advertises managed-session capabilities only for runtime
// kinds that the local user explicitly enabled. New scanners are detection-only.
func (s *Scanner) EnableTaskExecution(kinds ...string) {
	enabled := make(map[string]struct{}, len(kinds))
	for _, kind := range kinds {
		enabled[kind] = struct{}{}
	}
	for index := range s.Candidates {
		candidate := &s.Candidates[index]
		candidate.Capabilities = []string{protocol.CommandRuntimeProbe}
		if _, ok := enabled[candidate.Kind]; ok {
			candidate.Capabilities = append(
				[]string(nil),
				executionCapabilities[candidate.Kind]...,
			)
		}
	}
}

func (s *Scanner) Scan(ctx context.Context) protocol.InventorySnapshot {
	hostname, _ := s.Hostname()
	inventory := protocol.InventorySnapshot{
		ObservedAt: time.Now().UTC(),
		Host: protocol.HostInfo{
			Hostname:         hostname,
			OS:               runtime.GOOS,
			Architecture:     runtime.GOARCH,
			ConnectorVersion: s.Version,
		},
		Runtimes: []protocol.Runtime{},
	}

	seen := make(map[string]struct{})
	for _, candidate := range s.Candidates {
		path, err := s.find(candidate.Command)
		if err != nil {
			continue
		}
		path = canonicalPath(path)
		key := strings.ToLower(candidate.Kind + "\x00" + path)
		if _, duplicate := seen[key]; duplicate {
			continue
		}
		seen[key] = struct{}{}

		versionCtx, cancel := context.WithTimeout(ctx, s.Timeout)
		version, versionErr := s.ReadVersion(versionCtx, path)
		cancel()

		runtimeInfo := protocol.Runtime{
			ID:             stableRuntimeID(candidate.Kind, path),
			Kind:           candidate.Kind,
			DisplayName:    candidate.DisplayName,
			ExecutablePath: path,
			Version:        strings.TrimSpace(version),
			Status:         "ready",
			Available:      true,
			Capabilities:   append([]string(nil), candidate.Capabilities...),
			AuthModes:      append([]string(nil), candidate.AuthModes...),
			DetectedAt:     time.Now().UTC(),
		}
		if versionErr != nil {
			runtimeInfo.Status = "degraded"
			runtimeInfo.Available = false
			runtimeInfo.StatusDetail = "executable found but version probe failed: " + versionErr.Error()
			runtimeInfo.AuthenticationStatus = "unavailable"
		} else {
			authCtx, authCancel := context.WithTimeout(ctx, s.Timeout)
			authErr := s.ReadAuthStatus(authCtx, path, candidate.Kind)
			authCancel()
			if authErr == nil {
				runtimeInfo.AuthenticationStatus = "configured"
			} else {
				runtimeInfo.AuthenticationStatus = "unavailable"
			}
			compatibilityCtx, compatibilityCancel := context.WithTimeout(
				ctx,
				s.Timeout,
			)
			compatibilityErr := s.ReadArenaCompatibility(
				compatibilityCtx,
				path,
				candidate.Kind,
			)
			compatibilityCancel()
			runtimeInfo.ArenaCompatible = compatibilityErr == nil
		}
		runtimeInfo.TaskEnabled = contains(
			runtimeInfo.Capabilities,
			protocol.CommandTaskDispatch,
		)
		runtimeInfo.ArenaIsolation = "none"
		if runtimeInfo.TaskEnabled && runtimeInfo.ArenaCompatible {
			runtimeInfo.ArenaIsolation = arenaIsolationProfiles[candidate.Kind]
		}
		if !runtimeInfo.TaskEnabled {
			runtimeInfo.ReadinessIssues = append(
				runtimeInfo.ReadinessIssues,
				"task_execution_disabled",
			)
		}
		if runtimeInfo.AuthenticationStatus != "configured" {
			runtimeInfo.ReadinessIssues = append(
				runtimeInfo.ReadinessIssues,
				"authentication_unavailable",
			)
		}
		if !runtimeInfo.ArenaCompatible {
			runtimeInfo.ReadinessIssues = append(
				runtimeInfo.ReadinessIssues,
				"arena_profile_unsupported",
			)
		}
		if runtimeInfo.ArenaIsolation == "" ||
			runtimeInfo.ArenaIsolation == "none" {
			runtimeInfo.ReadinessIssues = append(
				runtimeInfo.ReadinessIssues,
				"arena_isolation_unavailable",
			)
		}
		runtimeInfo.LocalExecutionReady = runtimeInfo.Available &&
			runtimeInfo.TaskEnabled &&
			runtimeInfo.AuthenticationStatus == "configured" &&
			runtimeInfo.ArenaCompatible &&
			runtimeInfo.ArenaIsolation != "" &&
			runtimeInfo.ArenaIsolation != "none"
		inventory.Runtimes = append(inventory.Runtimes, runtimeInfo)
	}

	sort.Slice(inventory.Runtimes, func(i, j int) bool {
		return inventory.Runtimes[i].ID < inventory.Runtimes[j].ID
	})
	return inventory
}

func (s *Scanner) find(command string) (string, error) {
	if path, err := s.LookPath(command); err == nil {
		return path, nil
	}
	for _, directory := range s.AdditionalPaths {
		for _, name := range executableNames(command) {
			path := filepath.Join(directory, name)
			info, err := os.Stat(path)
			if err == nil && !info.IsDir() {
				return path, nil
			}
		}
	}
	return "", fmt.Errorf("%s not found", command)
}

func executableNames(command string) []string {
	if runtime.GOOS == "windows" {
		return []string{command + ".exe", command + ".cmd", command + ".bat", command}
	}
	return []string{command}
}

func defaultReadVersion(ctx context.Context, executable string) (string, error) {
	output, err := exec.CommandContext(ctx, executable, "--version").CombinedOutput()
	text := strings.TrimSpace(string(output))
	if len(text) > 512 {
		text = text[:512]
	}
	if err != nil {
		if text != "" {
			return text, fmt.Errorf("%w: %s", err, text)
		}
		return "", err
	}
	return text, nil
}

func defaultReadAuthStatus(ctx context.Context, executable, kind string) error {
	var args []string
	switch kind {
	case "codex":
		args = []string{"login", "status"}
	case "claude_code":
		args = []string{"auth", "status"}
	default:
		return fmt.Errorf("authentication status is unsupported for %s", kind)
	}
	return exec.CommandContext(ctx, executable, args...).Run()
}

func defaultReadArenaCompatibility(
	ctx context.Context,
	executable string,
	kind string,
) error {
	var args []string
	var required []string
	switch kind {
	case "codex":
		args = []string{"exec", "--help"}
		required = []string{
			"--sandbox",
			"--ephemeral",
			"--ignore-user-config",
			"--ignore-rules",
			"--output-schema",
			"--cd",
		}
	case "claude_code":
		args = []string{"--help"}
		required = []string{
			"--safe-mode",
			"--strict-mcp-config",
			"--tools",
			"--disable-slash-commands",
			"--no-session-persistence",
			"--permission-mode",
			"--json-schema",
		}
	default:
		return fmt.Errorf("Arena compatibility is unsupported for %s", kind)
	}
	output, err := exec.CommandContext(ctx, executable, args...).Output()
	if err != nil {
		return err
	}
	text := string(output)
	for _, flag := range required {
		if !strings.Contains(text, flag) {
			return fmt.Errorf("required Arena flag is unavailable")
		}
	}
	return nil
}

func contains(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func canonicalPath(path string) string {
	absolute, err := filepath.Abs(path)
	if err == nil {
		path = absolute
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err == nil {
		path = resolved
	}
	return filepath.Clean(path)
}

func stableRuntimeID(kind, path string) string {
	sum := sha256.Sum256([]byte(kind + "\x00" + strings.ToLower(filepath.Clean(path))))
	return "rt-" + hex.EncodeToString(sum[:10])
}

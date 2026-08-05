package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/discovery"
	"github.com/adx-agentic-payment/adx/connector/internal/driver"
	"github.com/adx-agentic-payment/adx/connector/internal/enrollment"
	"github.com/adx-agentic-payment/adx/connector/internal/store"
	"github.com/adx-agentic-payment/adx/connector/internal/supervisor"
	"github.com/adx-agentic-payment/adx/connector/internal/transport"
)

const connectorVersion = "0.1.0"

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "adx-connector:", err)
		os.Exit(exitCode(err))
	}
}

func run(arguments []string) error {
	if len(arguments) == 0 {
		printUsage()
		return errors.New("a subcommand is required")
	}
	switch arguments[0] {
	case "scan":
		return runScan(arguments[1:])
	case "doctor":
		return runDoctor(arguments[1:])
	case "pair":
		return runPair(arguments[1:])
	case "connect":
		return runConnector(arguments[1:], "connect")
	case "run":
		return runConnector(arguments[1:], "run")
	case "version", "--version", "-version":
		fmt.Println(connectorVersion)
		return nil
	case "help", "--help", "-h":
		printUsage()
		return nil
	default:
		printUsage()
		return fmt.Errorf("unknown subcommand %q", arguments[0])
	}
}

func runScan(arguments []string) error {
	flags := flag.NewFlagSet("scan", flag.ContinueOnError)
	timeout := flags.Duration("timeout", 3*time.Second, "per-runtime version probe timeout")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	scanner := newScanner(*timeout)
	return writeJSON(os.Stdout, scanner.Scan(context.Background()))
}

func runDoctor(arguments []string) error {
	defaultState, err := store.DefaultPath()
	if err != nil {
		return err
	}
	flags := flag.NewFlagSet("doctor", flag.ContinueOnError)
	statePath := flags.String("state", defaultState, "connector state file")
	timeout := flags.Duration("timeout", 3*time.Second, "per-runtime version probe timeout")
	if err := flags.Parse(arguments); err != nil {
		return err
	}

	type check struct {
		Name   string `json:"name"`
		Status string `json:"status"`
		Detail string `json:"detail"`
	}
	report := struct {
		OK     bool    `json:"ok"`
		Checks []check `json:"checks"`
	}{OK: true, Checks: []check{}}

	fileStore := store.NewFileStore(*statePath)
	credentials, credentialErr := fileStore.LoadCredentials()
	switch {
	case credentialErr == nil:
		report.Checks = append(report.Checks, check{
			Name:   "device_credentials",
			Status: "ok",
			Detail: "paired device " + credentials.DeviceID + "; token is stored but not displayed",
		})
	case errors.Is(credentialErr, store.ErrNotInitialized):
		report.Checks = append(report.Checks, check{
			Name:   "device_credentials",
			Status: "warning",
			Detail: "not paired; run `adx-connector pair` or `adx-connector run`",
		})
	default:
		report.OK = false
		report.Checks = append(report.Checks, check{
			Name:   "device_credentials",
			Status: "error",
			Detail: credentialErr.Error(),
		})
	}

	inventory := newScanner(*timeout).Scan(context.Background())
	if len(inventory.Runtimes) == 0 {
		report.OK = false
		report.Checks = append(report.Checks, check{
			Name:   "runtime_discovery",
			Status: "error",
			Detail: "neither Claude Code nor Codex was found on PATH or common install paths",
		})
	} else {
		report.Checks = append(report.Checks, check{
			Name:   "runtime_discovery",
			Status: "ok",
			Detail: fmt.Sprintf("detected %d supported runtime(s)", len(inventory.Runtimes)),
		})
		for _, runtimeInfo := range inventory.Runtimes {
			status := runtimeInfo.Status
			if status == "degraded" {
				report.OK = false
			}
			report.Checks = append(report.Checks, check{
				Name:   "runtime_" + runtimeInfo.Kind,
				Status: status,
				Detail: runtimeInfo.ExecutablePath + " " + runtimeInfo.Version,
			})
		}
	}
	return writeJSON(os.Stdout, report)
}

func runPair(arguments []string) error {
	defaultState, err := store.DefaultPath()
	if err != nil {
		return err
	}
	flags := flag.NewFlagSet("pair", flag.ContinueOnError)
	server := flags.String("server", "", "ADX platform HTTPS origin")
	apiBase := flags.String("api-base", "", "deprecated alias for --server")
	statePath := flags.String("state", defaultState, "connector state file")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	selectedServer, err := selectServer(*server, *apiBase, configuredServer("http://localhost:8000"))
	if err != nil {
		return err
	}
	stateLock, err := store.AcquireStateLock(*statePath)
	if err != nil {
		return fmt.Errorf("acquire connector state lock: %w", err)
	}
	defer stateLock.Close()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	credentials, err := pair(ctx, selectedServer)
	if err != nil {
		return err
	}
	if err := store.NewFileStore(*statePath).SaveCredentials(credentials); err != nil {
		return err
	}
	fmt.Printf("Paired device %s. Credentials saved to %s\n", credentials.DeviceID, *statePath)
	return nil
}

func runConnector(arguments []string, commandName string) error {
	defaultState, err := store.DefaultPath()
	if err != nil {
		return err
	}
	flags := flag.NewFlagSet(commandName, flag.ContinueOnError)
	server := flags.String("server", "", "ADX platform HTTPS origin")
	apiBase := flags.String("api-base", "", "deprecated alias for --server")
	statePath := flags.String("state", defaultState, "connector state file")
	gatewayOverride := flags.String("gateway", envOr("ADX_CONNECTOR_GATEWAY_URL", ""), "override the paired websocket gateway URL")
	taskTransport := flags.String(
		"task-transport",
		envOr("ADX_CONNECTOR_TASK_TRANSPORT", "wss"),
		"Arena task transport: wss or mcp",
	)
	autoPair := flags.Bool("auto-pair", true, "start device pairing when credentials are missing")
	heartbeat := flags.Duration("heartbeat", 15*time.Second, "heartbeat interval")
	inventoryInterval := flags.Duration("inventory-interval", time.Minute, "automatic runtime rescan interval")
	discoveryTimeout := flags.Duration("discovery-timeout", 3*time.Second, "per-runtime version probe timeout")
	enableCodexTasks := flags.Bool(
		"enable-codex-tasks",
		envEnabled("ADX_CONNECTOR_ENABLE_CODEX_TASKS"),
		"allow this Connector to start Codex tasks inside allow-root workspaces",
	)
	enableClaudeTasks := flags.Bool(
		"unsafe-enable-claude-tasks",
		envEnabled("ADX_CONNECTOR_UNSAFE_ENABLE_CLAUDE_TASKS"),
		"development only: allow Claude Code tasks using unverified local authentication",
	)
	var roots stringList
	var allowedEnvironment stringList
	var runtimeKinds stringList
	flags.Var(&roots, "allow-root", "working directory root allowed for managed sessions; may be repeated")
	flags.Var(
		&runtimeKinds,
		"runtime-kind",
		"discover only this Runtime kind (codex or claude_code); may be repeated",
	)
	flags.Var(
		&allowedEnvironment,
		"allow-env",
		"local environment variable name that an environment_refs payload may expose; may be repeated",
	)
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	fallbackServer := configuredServer("http://localhost:8000")
	if commandName == "connect" {
		fallbackServer = configuredServer("")
	}
	selectedServer, err := selectServer(*server, *apiBase, fallbackServer)
	if err != nil {
		return err
	}
	if commandName == "connect" && selectedServer == "" {
		return errors.New("connect requires --server https://arena.example")
	}
	stateLock, err := store.AcquireStateLock(*statePath)
	if err != nil {
		return fmt.Errorf("acquire connector state lock: %w", err)
	}
	defer stateLock.Close()

	if len(roots) == 0 {
		current, err := os.Getwd()
		if err != nil {
			return err
		}
		roots = append(roots, current)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	fileStore := store.NewFileStore(*statePath)
	credentials, err := loadCredentials(ctx, fileStore, selectedServer, *gatewayOverride, *autoPair)
	if err != nil {
		return err
	}

	runtimeScanner := newScanner(*discoveryTimeout)
	if len(runtimeKinds) > 0 {
		if err := runtimeScanner.RestrictKinds(runtimeKinds...); err != nil {
			return err
		}
	}
	enabledDrivers := make([]driver.Driver, 0, 2)
	enabledKinds := make([]string, 0, 2)
	if *enableCodexTasks {
		enabledKinds = append(enabledKinds, "codex")
		enabledDrivers = append(enabledDrivers, driver.CodexDriver{})
	}
	if *enableClaudeTasks {
		enabledKinds = append(enabledKinds, "claude_code")
		enabledDrivers = append(enabledDrivers, driver.ClaudeDriver{})
	}
	runtimeScanner.EnableTaskExecution(enabledKinds...)
	inventory := runtimeScanner.Scan(ctx)
	processSupervisor, err := supervisor.New(
		runtimeScanner,
		fileStore,
		driver.NewRegistry(enabledDrivers...),
		roots,
		allowedEnvironment,
		inventory,
	)
	if err != nil {
		return err
	}
	defer processSupervisor.Shutdown()

	logger := log.New(os.Stderr, "adx-connector: ", log.LstdFlags|log.LUTC)
	if *enableClaudeTasks {
		logger.Print(
			"WARNING: Claude Code task execution is enabled with unverified local authentication; " +
				"use only in an isolated development account after vendor approval",
		)
	}
	client, err := transport.NewClient(
		transport.Config{
			Credentials:       credentials,
			ConnectorVersion:  connectorVersion,
			HeartbeatInterval: *heartbeat,
			InventoryInterval: *inventoryInterval,
			TaskTransport:     *taskTransport,
		},
		fileStore,
		store.NewFileOutbox(*statePath),
		processSupervisor,
		logger,
	)
	if err != nil {
		return err
	}
	fmt.Printf(
		"Connector %s starting for device %s with %d detected runtime(s); task transport: %s; task-enabled runtimes: %s; allowed roots: %s\n",
		connectorVersion,
		credentials.DeviceID,
		len(inventory.Runtimes),
		strings.ToLower(strings.TrimSpace(*taskTransport)),
		enabledRuntimeSummary(*enableCodexTasks, *enableClaudeTasks),
		strings.Join(roots, ", "),
	)
	err = client.Run(ctx)
	if errors.Is(err, context.Canceled) {
		return nil
	}
	return err
}

func loadCredentials(
	ctx context.Context,
	fileStore *store.FileStore,
	apiBase string,
	gatewayOverride string,
	autoPair bool,
) (store.Credentials, error) {
	credentials, err := credentialsFromEnvironment()
	if err != nil {
		return store.Credentials{}, err
	}
	if credentials.DeviceID == "" {
		credentials, err = fileStore.LoadCredentials()
	}
	if errors.Is(err, store.ErrNotInitialized) {
		if !autoPair {
			return store.Credentials{}, errors.New("connector is not paired and --auto-pair=false")
		}
		credentials, err = pair(ctx, apiBase)
	}
	if err != nil {
		return store.Credentials{}, err
	}
	if gatewayOverride != "" {
		credentials.GatewayURL = gatewayOverride
	}
	if err := credentials.Validate(); err != nil {
		return store.Credentials{}, err
	}
	if err := fileStore.SaveCredentials(credentials); err != nil {
		return store.Credentials{}, err
	}
	return credentials, nil
}

func credentialsFromEnvironment() (store.Credentials, error) {
	deviceID := strings.TrimSpace(os.Getenv("ADX_CONNECTOR_DEVICE_ID"))
	token := strings.TrimSpace(os.Getenv("ADX_CONNECTOR_TOKEN"))
	gatewayURL := strings.TrimSpace(os.Getenv("ADX_CONNECTOR_GATEWAY_URL"))
	if deviceID == "" && token == "" {
		return store.Credentials{}, nil
	}
	if deviceID == "" || token == "" || gatewayURL == "" {
		return store.Credentials{}, errors.New(
			"ADX_CONNECTOR_DEVICE_ID, ADX_CONNECTOR_TOKEN, and ADX_CONNECTOR_GATEWAY_URL must be set together",
		)
	}
	return store.Credentials{DeviceID: deviceID, Token: token, GatewayURL: gatewayURL}, nil
}

func pair(ctx context.Context, apiBase string) (store.Credentials, error) {
	client := &enrollment.Client{
		BaseURL:          apiBase,
		ConnectorVersion: connectorVersion,
		Output:           os.Stdout,
	}
	return client.Pair(ctx)
}

func newScanner(timeout time.Duration) *discovery.Scanner {
	scanner := discovery.NewScanner(connectorVersion, timeout)
	scanner.AdditionalPaths = defaultAdditionalPaths()
	return scanner
}

func defaultAdditionalPaths() []string {
	paths := []string{}
	home, _ := os.UserHomeDir()
	if home != "" {
		paths = append(
			paths,
			filepath.Join(home, ".local", "bin"),
			filepath.Join(home, ".npm-global", "bin"),
			filepath.Join(home, ".local", "share", "pnpm"),
			filepath.Join(home, ".bun", "bin"),
			filepath.Join(home, ".cargo", "bin"),
		)
	}
	if runtime.GOOS == "windows" {
		if value := os.Getenv("APPDATA"); value != "" {
			paths = append(paths, filepath.Join(value, "npm"))
		}
		if value := os.Getenv("LOCALAPPDATA"); value != "" {
			paths = append(paths, filepath.Join(value, "npm"), filepath.Join(value, "pnpm"))
		}
	} else if home != "" {
		paths = appendVersionedBinaryDirectories(
			paths,
			filepath.Join(home, ".nvm", "versions", "node"),
			"bin",
		)
		paths = appendVersionedBinaryDirectories(
			paths,
			filepath.Join(home, ".local", "share", "fnm", "node-versions"),
			"installation",
			"bin",
		)
	}
	return paths
}

func appendVersionedBinaryDirectories(paths []string, root string, suffix ...string) []string {
	entries, err := os.ReadDir(root)
	if err != nil {
		return paths
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		parts := append([]string{root, entry.Name()}, suffix...)
		paths = append(paths, filepath.Join(parts...))
	}
	return paths
}

func writeJSON(output *os.File, value any) error {
	encoder := json.NewEncoder(output)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func configuredServer(fallback string) string {
	if value := strings.TrimSpace(os.Getenv("ADX_SERVER")); value != "" {
		return value
	}
	return envOr("ADX_API_BASE", fallback)
}

func selectServer(server, apiBase, fallback string) (string, error) {
	server = strings.TrimSpace(server)
	apiBase = strings.TrimSpace(apiBase)
	if server != "" && apiBase != "" && strings.TrimRight(server, "/") != strings.TrimRight(apiBase, "/") {
		return "", errors.New("--server and --api-base must refer to the same platform")
	}
	switch {
	case server != "":
		return server, nil
	case apiBase != "":
		return apiBase, nil
	default:
		return strings.TrimSpace(fallback), nil
	}
}

func exitCode(err error) int {
	if errors.Is(err, transport.ErrDeviceRevoked) ||
		errors.Is(err, transport.ErrConnectionReplaced) {
		return 78
	}
	return 1
}

func envEnabled(name string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(name))) {
	case "1", "true", "yes":
		return true
	default:
		return false
	}
}

func enabledRuntimeSummary(codex, claude bool) string {
	enabled := make([]string, 0, 2)
	if codex {
		enabled = append(enabled, "codex")
	}
	if claude {
		enabled = append(enabled, "claude_code (unsafe development mode)")
	}
	if len(enabled) == 0 {
		return "none (detection-only)"
	}
	return strings.Join(enabled, ", ")
}

type stringList []string

func (values *stringList) String() string {
	return strings.Join(*values, ",")
}

func (values *stringList) Set(value string) error {
	if strings.TrimSpace(value) == "" {
		return errors.New("allow-root cannot be empty")
	}
	*values = append(*values, value)
	return nil
}

func printUsage() {
	fmt.Print(`ADX local Connector

Usage:
  adx-connector scan [flags]    Detect supported local agent runtimes
  adx-connector doctor [flags]  Check pairing and runtime readiness
  adx-connector pair [flags]    Enroll this device with the ADX platform
  adx-connector connect --server https://arena.example
                                Authorize once, then stay online
  adx-connector run [flags]     Connect, report inventory, and run managed sessions
  adx-connector version         Print the connector version

The connector starts in detection-only mode. Task execution requires a local
runtime-specific opt-in flag. It accepts only typed runtime/session/task commands
and never accepts an executable path, process arguments, or arbitrary shell text
from the cloud. Agent prompts can still cause the enabled runtime to use its own
tools, so enable task execution only for a trusted platform and workspace.
`)
}

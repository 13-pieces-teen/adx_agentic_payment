package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
)

var ErrNotInitialized = errors.New("connector state is not initialized")
var ErrEventAlreadyStaged = errors.New("an event is already staged for outbox recovery")

const (
	schemaVersion              = 1
	maxTerminalCommandReceipts = 512
)

type Credentials struct {
	DeviceID   string    `json:"device_id"`
	Token      string    `json:"token"`
	GatewayURL string    `json:"gateway_url"`
	UpdatedAt  time.Time `json:"updated_at"`
}

func (c Credentials) Validate() error {
	if strings.TrimSpace(c.DeviceID) == "" {
		return errors.New("device_id is required")
	}
	if strings.TrimSpace(c.Token) == "" {
		return errors.New("device token is required")
	}
	if strings.TrimSpace(c.GatewayURL) == "" {
		return errors.New("gateway_url is required")
	}
	return nil
}

type state struct {
	SchemaVersion   int                            `json:"schema_version"`
	Credentials     Credentials                    `json:"credentials"`
	NextSequence    uint64                         `json:"next_sequence"`
	StagedEvent     *protocol.RuntimeEvent         `json:"staged_event,omitempty"`
	CommandReceipts map[string]protocol.CommandAck `json:"command_receipts,omitempty"`
}

type FileStore struct {
	path string
	mu   sync.Mutex
}

func NewFileStore(path string) *FileStore {
	return &FileStore{path: path}
}

func DefaultPath() (string, error) {
	directory, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("resolve user config directory: %w", err)
	}
	return filepath.Join(directory, "adx", "connector", "state.json"), nil
}

func (s *FileStore) Path() string {
	return s.path
}

func (s *FileStore) LoadCredentials() (Credentials, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if err != nil {
		return Credentials{}, err
	}
	if err := current.Credentials.Validate(); err != nil {
		return Credentials{}, ErrNotInitialized
	}
	return current.Credentials, nil
}

func (s *FileStore) SaveCredentials(credentials Credentials) error {
	if err := credentials.Validate(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if err != nil && !errors.Is(err, ErrNotInitialized) {
		return err
	}
	if errors.Is(err, ErrNotInitialized) {
		current = newState()
	}
	credentials.UpdatedAt = time.Now().UTC()
	current.Credentials = credentials
	return s.save(current)
}

func (s *FileStore) StageEvent(event protocol.RuntimeEvent) (protocol.RuntimeEvent, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if err != nil {
		if !errors.Is(err, ErrNotInitialized) {
			return protocol.RuntimeEvent{}, err
		}
		current = newState()
	}
	if current.StagedEvent != nil {
		return protocol.RuntimeEvent{}, ErrEventAlreadyStaged
	}
	if event.Sequence != 0 {
		return protocol.RuntimeEvent{}, errors.New("new staged event must not already have a sequence")
	}
	current.NextSequence++
	event.Sequence = current.NextSequence
	current.StagedEvent = &event
	if err := s.save(current); err != nil {
		return protocol.RuntimeEvent{}, err
	}
	return event, nil
}

func (s *FileStore) StagedEvent() (*protocol.RuntimeEvent, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if errors.Is(err, ErrNotInitialized) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if current.StagedEvent == nil {
		return nil, nil
	}
	event := *current.StagedEvent
	return &event, nil
}

func (s *FileStore) ClearStagedEvent(sequence uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if errors.Is(err, ErrNotInitialized) {
		return ErrNotInitialized
	}
	if err != nil {
		return err
	}
	if current.StagedEvent == nil {
		return nil
	}
	if current.StagedEvent.Sequence != sequence {
		return fmt.Errorf(
			"staged event sequence mismatch: have %d, clearing %d",
			current.StagedEvent.Sequence,
			sequence,
		)
	}
	current.StagedEvent = nil
	return s.save(current)
}

func (s *FileStore) LookupReceipt(idempotencyKey string) (protocol.CommandAck, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if errors.Is(err, ErrNotInitialized) {
		return protocol.CommandAck{}, false, nil
	}
	if err != nil {
		return protocol.CommandAck{}, false, err
	}
	receipt, ok := current.CommandReceipts[idempotencyKey]
	return receipt, ok, nil
}

func (s *FileStore) SaveReceipt(idempotencyKey string, receipt protocol.CommandAck) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if err != nil && !errors.Is(err, ErrNotInitialized) {
		return err
	}
	if errors.Is(err, ErrNotInitialized) {
		current = newState()
	}
	if current.CommandReceipts == nil {
		current.CommandReceipts = make(map[string]protocol.CommandAck)
	}
	current.CommandReceipts[idempotencyKey] = receipt
	pruneReceipts(current.CommandReceipts)
	return s.save(current)
}

func (s *FileStore) Receipts() ([]protocol.CommandAck, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if errors.Is(err, ErrNotInitialized) {
		return []protocol.CommandAck{}, nil
	}
	if err != nil {
		return nil, err
	}
	receipts := make([]protocol.CommandAck, 0, len(current.CommandReceipts))
	for _, receipt := range current.CommandReceipts {
		receipts = append(receipts, receipt)
	}
	sort.Slice(receipts, func(i, j int) bool {
		return receipts[i].RecordedAt.Before(receipts[j].RecordedAt)
	})
	return receipts, nil
}

func (s *FileStore) RecoverInterruptedReceipts() (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	current, err := s.load()
	if errors.Is(err, ErrNotInitialized) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	recovered := 0
	recordedAt := time.Now().UTC()
	for key, receipt := range current.CommandReceipts {
		if receipt.Status != "accepted" {
			continue
		}
		receipt.Status = "failed"
		receipt.Code = "connector_restarted"
		receipt.Message = "connector restarted before the command reached a terminal state"
		receipt.RecordedAt = recordedAt
		current.CommandReceipts[key] = receipt
		recovered++
	}
	if recovered == 0 {
		return 0, nil
	}
	if err := s.save(current); err != nil {
		return 0, err
	}
	return recovered, nil
}

func (s *FileStore) load() (state, error) {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return state{}, ErrNotInitialized
	}
	if err != nil {
		return state{}, fmt.Errorf("read connector state: %w", err)
	}
	var current state
	if err := json.Unmarshal(data, &current); err != nil {
		return state{}, fmt.Errorf("decode connector state: %w", err)
	}
	if current.SchemaVersion != schemaVersion {
		return state{}, fmt.Errorf("unsupported connector state schema %d", current.SchemaVersion)
	}
	if current.CommandReceipts == nil {
		current.CommandReceipts = make(map[string]protocol.CommandAck)
	}
	return current, nil
}

func (s *FileStore) save(current state) error {
	if current.SchemaVersion == 0 {
		current.SchemaVersion = schemaVersion
	}
	if current.CommandReceipts == nil {
		current.CommandReceipts = make(map[string]protocol.CommandAck)
	}
	data, err := json.MarshalIndent(current, "", "  ")
	if err != nil {
		return fmt.Errorf("encode connector state: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return fmt.Errorf("create connector state directory: %w", err)
	}
	temp, err := os.CreateTemp(filepath.Dir(s.path), ".state-*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary connector state: %w", err)
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	if err := temp.Chmod(0o600); err != nil {
		temp.Close()
		return fmt.Errorf("protect temporary connector state: %w", err)
	}
	if _, err := temp.Write(data); err != nil {
		temp.Close()
		return fmt.Errorf("write temporary connector state: %w", err)
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return fmt.Errorf("sync temporary connector state: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("close temporary connector state: %w", err)
	}
	if err := os.Rename(tempPath, s.path); err != nil {
		return fmt.Errorf("replace connector state: %w", err)
	}
	return os.Chmod(s.path, 0o600)
}

func newState() state {
	return state{
		SchemaVersion:   schemaVersion,
		CommandReceipts: make(map[string]protocol.CommandAck),
	}
}

func pruneReceipts(receipts map[string]protocol.CommandAck) {
	type entry struct {
		key string
		at  time.Time
	}
	terminal := make([]entry, 0, len(receipts))
	for key, receipt := range receipts {
		if isTerminalReceipt(receipt.Status) {
			terminal = append(terminal, entry{key: key, at: receipt.RecordedAt})
		}
	}
	if len(terminal) <= maxTerminalCommandReceipts {
		return
	}
	sort.Slice(terminal, func(i, j int) bool {
		if terminal[i].at.Equal(terminal[j].at) {
			return terminal[i].key < terminal[j].key
		}
		return terminal[i].at.Before(terminal[j].at)
	})
	for _, item := range terminal[:len(terminal)-maxTerminalCommandReceipts] {
		delete(receipts, item.key)
	}
}

func isTerminalReceipt(status string) bool {
	switch status {
	case "completed", "failed", "rejected":
		return true
	default:
		// Unknown states are retained fail-closed. A newly introduced non-terminal
		// state must never be evicted merely because this version does not know it.
		return false
	}
}

type FileOutbox struct {
	directory string
	mu        sync.Mutex
}

func NewFileOutbox(statePath string) *FileOutbox {
	return &FileOutbox{directory: filepath.Join(filepath.Dir(statePath), "outbox")}
}

func (o *FileOutbox) Append(event protocol.RuntimeEvent) error {
	if event.Sequence == 0 {
		return errors.New("event sequence is required")
	}
	o.mu.Lock()
	defer o.mu.Unlock()

	if err := os.MkdirAll(o.directory, 0o700); err != nil {
		return fmt.Errorf("create event outbox: %w", err)
	}
	data, err := json.Marshal(event)
	if err != nil {
		return fmt.Errorf("encode event: %w", err)
	}
	target := filepath.Join(o.directory, eventFilename(event.Sequence))
	existing, err := os.ReadFile(target)
	if err == nil {
		if bytes.Equal(existing, data) {
			return nil
		}
		return fmt.Errorf("outbox sequence %d already contains a different event", event.Sequence)
	}
	if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("read existing outbox event: %w", err)
	}
	temp, err := os.CreateTemp(o.directory, ".event-*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary outbox event: %w", err)
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	if err := temp.Chmod(0o600); err != nil {
		temp.Close()
		return err
	}
	if _, err := temp.Write(data); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tempPath, target); err != nil {
		return fmt.Errorf("commit outbox event: %w", err)
	}
	return os.Chmod(target, 0o600)
}

func (o *FileOutbox) Pending() ([]protocol.RuntimeEvent, error) {
	o.mu.Lock()
	defer o.mu.Unlock()

	entries, err := os.ReadDir(o.directory)
	if errors.Is(err, os.ErrNotExist) {
		return []protocol.RuntimeEvent{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read event outbox: %w", err)
	}
	events := make([]protocol.RuntimeEvent, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(o.directory, entry.Name()))
		if err != nil {
			return nil, err
		}
		var event protocol.RuntimeEvent
		if err := json.Unmarshal(data, &event); err != nil {
			return nil, fmt.Errorf("decode outbox event %s: %w", entry.Name(), err)
		}
		events = append(events, event)
	}
	sort.Slice(events, func(i, j int) bool {
		return events[i].Sequence < events[j].Sequence
	})
	return events, nil
}

func (o *FileOutbox) AckThrough(sequence uint64) error {
	o.mu.Lock()
	defer o.mu.Unlock()

	entries, err := os.ReadDir(o.directory)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read event outbox: %w", err)
	}
	for _, entry := range entries {
		eventSequence, ok := parseEventFilename(entry.Name())
		if !ok || eventSequence > sequence {
			continue
		}
		if err := os.Remove(filepath.Join(o.directory, entry.Name())); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("remove acknowledged outbox event: %w", err)
		}
	}
	return nil
}

func eventFilename(sequence uint64) string {
	return fmt.Sprintf("%020d.json", sequence)
}

func parseEventFilename(name string) (uint64, bool) {
	if !strings.HasSuffix(name, ".json") {
		return 0, false
	}
	value, err := strconv.ParseUint(strings.TrimSuffix(name, ".json"), 10, 64)
	return value, err == nil
}

package store

import (
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
)

func TestFileStoreRoundTripsCredentialsStagedEventAndReceipt(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	fileStore := NewFileStore(path)
	if _, err := fileStore.LoadCredentials(); !errors.Is(err, ErrNotInitialized) {
		t.Fatalf("expected uninitialized state, got %v", err)
	}

	credentials := Credentials{
		DeviceID:   "device-1",
		Token:      "secret-device-token",
		GatewayURL: "wss://example.test/connectors/ws",
	}
	if err := fileStore.SaveCredentials(credentials); err != nil {
		t.Fatal(err)
	}
	loaded, err := fileStore.LoadCredentials()
	if err != nil {
		t.Fatal(err)
	}
	if loaded.DeviceID != credentials.DeviceID || loaded.Token != credentials.Token {
		t.Fatalf("unexpected credentials: %#v", loaded)
	}

	first, err := fileStore.StageEvent(
		protocol.NewRuntimeEvent("runtime-1", "session-1", "task-1", "runtime.stdout", nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	if first.Sequence != 1 {
		t.Fatalf("unexpected first sequence: %d", first.Sequence)
	}
	if _, err := fileStore.StageEvent(
		protocol.NewRuntimeEvent("runtime-1", "session-1", "task-2", "runtime.stdout", nil),
	); !errors.Is(err, ErrEventAlreadyStaged) {
		t.Fatalf("second event must wait for staged event recovery, got %v", err)
	}
	if err := fileStore.ClearStagedEvent(first.Sequence); err != nil {
		t.Fatal(err)
	}
	second, err := fileStore.StageEvent(
		protocol.NewRuntimeEvent("runtime-1", "session-1", "task-2", "runtime.stdout", nil),
	)
	if err != nil {
		t.Fatal(err)
	}
	if second.Sequence != 2 {
		t.Fatalf("unexpected second sequence: %d", second.Sequence)
	}

	receipt := protocol.CommandAck{
		CommandID:      "cmd-1",
		IdempotencyKey: "idem-1",
		Status:         "accepted",
		RecordedAt:     time.Now().UTC(),
	}
	if err := fileStore.SaveReceipt("idem-1", receipt); err != nil {
		t.Fatal(err)
	}
	loadedReceipt, found, err := fileStore.LookupReceipt("idem-1")
	if err != nil || !found || loadedReceipt.CommandID != "cmd-1" {
		t.Fatalf("unexpected receipt: %#v, %v, %v", loadedReceipt, found, err)
	}
	receipts, err := fileStore.Receipts()
	if err != nil || len(receipts) != 1 || receipts[0].CommandID != "cmd-1" {
		t.Fatalf("unexpected receipt replay set: %#v, %v", receipts, err)
	}
}

func TestFileOutboxOrdersAndAcknowledgesEvents(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "state.json")
	outbox := NewFileOutbox(statePath)
	for _, sequence := range []uint64{3, 1, 2} {
		event := protocol.NewRuntimeEvent("runtime-1", "session-1", "task-1", "runtime.stdout", nil)
		event.Sequence = sequence
		if err := outbox.Append(event); err != nil {
			t.Fatal(err)
		}
	}
	pending, err := outbox.Pending()
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 3 || pending[0].Sequence != 1 || pending[2].Sequence != 3 {
		t.Fatalf("unexpected pending events: %#v", pending)
	}
	if err := outbox.AckThrough(2); err != nil {
		t.Fatal(err)
	}
	pending, err = outbox.Pending()
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 1 || pending[0].Sequence != 3 {
		t.Fatalf("unexpected pending events after ack: %#v", pending)
	}
}

func TestRecoverInterruptedReceiptsMakesAcceptedReceiptTerminal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	fileStore := NewFileStore(path)
	accepted := protocol.CommandAck{
		CommandID:      "cmd-running",
		IdempotencyKey: "idem-running",
		Status:         "accepted",
		RecordedAt:     time.Now().Add(-time.Minute).UTC(),
	}
	completed := protocol.CommandAck{
		CommandID:      "cmd-completed",
		IdempotencyKey: "idem-completed",
		Status:         "completed",
		RecordedAt:     time.Now().Add(-time.Minute).UTC(),
	}
	if err := fileStore.SaveReceipt("running", accepted); err != nil {
		t.Fatal(err)
	}
	if err := fileStore.SaveReceipt("completed", completed); err != nil {
		t.Fatal(err)
	}

	recovered, err := fileStore.RecoverInterruptedReceipts()
	if err != nil {
		t.Fatal(err)
	}
	if recovered != 1 {
		t.Fatalf("expected one recovered receipt, got %d", recovered)
	}
	interrupted, found, err := fileStore.LookupReceipt("running")
	if err != nil || !found {
		t.Fatalf("load interrupted receipt: found=%v err=%v", found, err)
	}
	if interrupted.Status != "failed" || interrupted.Code != "connector_restarted" {
		t.Fatalf("accepted receipt was not made terminal: %#v", interrupted)
	}
	unchanged, found, err := fileStore.LookupReceipt("completed")
	if err != nil || !found {
		t.Fatalf("load completed receipt: found=%v err=%v", found, err)
	}
	if unchanged.Status != "completed" {
		t.Fatalf("terminal receipt changed unexpectedly: %#v", unchanged)
	}
	recovered, err = fileStore.RecoverInterruptedReceipts()
	if err != nil || recovered != 0 {
		t.Fatalf("receipt recovery must be idempotent: recovered=%d err=%v", recovered, err)
	}
}

func TestFileOutboxAppendIsIdempotentButRejectsSequenceCollision(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "state.json")
	outbox := NewFileOutbox(statePath)
	event := protocol.NewRuntimeEvent("runtime-1", "session-1", "task-1", "runtime.stdout", nil)
	event.Sequence = 1
	if err := outbox.Append(event); err != nil {
		t.Fatal(err)
	}
	if err := outbox.Append(event); err != nil {
		t.Fatalf("re-appending the staged event must be idempotent: %v", err)
	}
	collision := protocol.NewRuntimeEvent("runtime-1", "session-1", "task-2", "runtime.stdout", nil)
	collision.Sequence = 1
	if err := outbox.Append(collision); err == nil {
		t.Fatal("different event with the same sequence must be rejected")
	}
}

func TestReceiptPruningRetainsEveryNonTerminalAndNewestTerminalReceipts(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	fileStore := NewFileStore(path)
	base := time.Now().Add(-24 * time.Hour).UTC()

	nonTerminal := map[string]string{
		"accepted-oldest": "accepted",
		"unknown-state":   "awaiting-runtime",
	}
	for key, status := range nonTerminal {
		if err := fileStore.SaveReceipt(key, protocol.CommandAck{
			CommandID:      key,
			IdempotencyKey: key,
			Status:         status,
			RecordedAt:     base.Add(-time.Hour),
		}); err != nil {
			t.Fatal(err)
		}
	}

	const overflow = 7
	for index := 0; index < maxTerminalCommandReceipts+overflow; index++ {
		key := fmt.Sprintf("terminal-%04d", index)
		statuses := []string{"completed", "failed", "rejected"}
		if err := fileStore.SaveReceipt(key, protocol.CommandAck{
			CommandID:      key,
			IdempotencyKey: key,
			Status:         statuses[index%len(statuses)],
			RecordedAt:     base.Add(time.Duration(index) * time.Second),
		}); err != nil {
			t.Fatal(err)
		}
	}

	for key, wantStatus := range nonTerminal {
		receipt, found, err := fileStore.LookupReceipt(key)
		if err != nil || !found {
			t.Fatalf("non-terminal receipt %q was pruned: found=%v err=%v", key, found, err)
		}
		if receipt.Status != wantStatus {
			t.Fatalf("non-terminal receipt %q changed: %#v", key, receipt)
		}
	}
	for index := 0; index < overflow; index++ {
		key := fmt.Sprintf("terminal-%04d", index)
		if _, found, err := fileStore.LookupReceipt(key); err != nil || found {
			t.Fatalf("old terminal receipt %q was not pruned: found=%v err=%v", key, found, err)
		}
	}
	for index := overflow; index < maxTerminalCommandReceipts+overflow; index++ {
		key := fmt.Sprintf("terminal-%04d", index)
		if _, found, err := fileStore.LookupReceipt(key); err != nil || !found {
			t.Fatalf("new terminal receipt %q should be retained: found=%v err=%v", key, found, err)
		}
	}

	receipts, err := fileStore.Receipts()
	if err != nil {
		t.Fatal(err)
	}
	if want := maxTerminalCommandReceipts + len(nonTerminal); len(receipts) != want {
		t.Fatalf("receipt count = %d, want %d", len(receipts), want)
	}
}

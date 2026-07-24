package redact

import "testing"

func TestTextRedactsKnownCredentialShapes(t *testing.T) {
	input := "Authorization: Bearer abc.def.ghi and sk-ant-api03-thisisasecretvalue"
	output := Text(input)
	if output == input || output == "" {
		t.Fatalf("expected redaction, got %q", output)
	}
}

func TestValueRedactsSecretKeysRecursively(t *testing.T) {
	value := map[string]any{
		"session_id": "safe-id",
		"nested": map[string]any{
			"api_key": "top-secret",
		},
	}
	redacted := Value(value).(map[string]any)
	if redacted["session_id"] != "safe-id" {
		t.Fatal("non-secret field should be preserved")
	}
	nested := redacted["nested"].(map[string]any)
	if nested["api_key"] != "[REDACTED]" {
		t.Fatal("secret field should be redacted")
	}
}

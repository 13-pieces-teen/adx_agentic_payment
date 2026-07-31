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

func TestValueDropsOptionalPrivateReasoningButKeepsPublicOutput(t *testing.T) {
	value := map[string]any{
		"type": "assistant",
		"content": []any{
			map[string]any{
				"type":      "thinking",
				"thinking":  "private reasoning",
				"signature": "private signature",
			},
			map[string]any{
				"type": "text",
				"text": `{"action":"buy","good":"grain"}`,
			},
		},
	}

	redacted := Value(value).(map[string]any)
	content := redacted["content"].([]any)
	privateBlock := content[0].(map[string]any)
	if len(privateBlock) != 1 || privateBlock["type"] != "thinking" {
		t.Fatalf("private reasoning block must retain only its type: %#v", privateBlock)
	}
	publicBlock := content[1].(map[string]any)
	if publicBlock["text"] != `{"action":"buy","good":"grain"}` {
		t.Fatalf("public structured output must be preserved: %#v", publicBlock)
	}
}

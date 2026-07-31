package redact

import (
	"regexp"
	"strings"
)

var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)\bsk-ant-[a-z0-9_-]{12,}\b`),
	regexp.MustCompile(`(?i)\bsk-[a-z0-9_-]{20,}\b`),
	regexp.MustCompile(`(?i)(authorization\s*:\s*bearer\s+)[a-z0-9._~+/=-]+`),
	regexp.MustCompile(`(?i)((?:api[_-]?key|access[_-]?token|secret|password)\s*["'=:\s]+\s*)[a-z0-9._~+/=-]{8,}`),
}

func Text(value string) string {
	redacted := value
	for _, pattern := range secretPatterns {
		redacted = pattern.ReplaceAllString(redacted, "${1}[REDACTED]")
	}
	return redacted
}

func Value(value any) any {
	switch typed := value.(type) {
	case string:
		return Text(typed)
	case []any:
		result := make([]any, len(typed))
		for index, item := range typed {
			result[index] = Value(item)
		}
		return result
	case map[string]any:
		if isPrivateReasoningBlock(typed) {
			return map[string]any{"type": typed["type"]}
		}
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			if isPrivateReasoningKey(key) {
				continue
			}
			if isSecretKey(key) {
				result[key] = "[REDACTED]"
				continue
			}
			result[key] = Value(item)
		}
		return result
	default:
		return value
	}
}

func isSecretKey(key string) bool {
	normalized := strings.ToLower(strings.ReplaceAll(key, "-", "_"))
	for _, marker := range []string{"api_key", "access_token", "authorization", "password", "secret"} {
		if strings.Contains(normalized, marker) {
			return true
		}
	}
	return false
}

func isPrivateReasoningBlock(value map[string]any) bool {
	blockType, _ := value["type"].(string)
	switch strings.ToLower(strings.TrimSpace(blockType)) {
	case "analysis", "reasoning", "thinking", "redacted_thinking":
		return true
	default:
		return false
	}
}

func isPrivateReasoningKey(key string) bool {
	normalized := strings.ToLower(strings.ReplaceAll(key, "-", "_"))
	switch normalized {
	case "analysis", "reasoning", "reasoning_content", "thinking":
		return true
	default:
		return false
	}
}

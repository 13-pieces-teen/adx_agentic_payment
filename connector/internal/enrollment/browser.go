package enrollment

import (
	"errors"
	"net/url"
	"os/exec"
	"runtime"
)

// openBrowser starts the operating system's URL handler without invoking a
// shell. The verification URL is still validated by Pair before it reaches
// this function.
func openBrowser(raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil {
		return err
	}
	if parsed.Scheme != "https" && parsed.Scheme != "http" {
		return errors.New("browser URL must use http or https")
	}

	var command *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		command = exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", raw)
	case "darwin":
		command = exec.Command("open", raw)
	default:
		command = exec.Command("xdg-open", raw)
	}
	if err := command.Start(); err != nil {
		return err
	}
	go func() {
		_ = command.Wait()
	}()
	return nil
}

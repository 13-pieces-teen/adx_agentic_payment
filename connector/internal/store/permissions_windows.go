//go:build windows

package store

import (
	"os"

	"golang.org/x/sys/windows"
)

func secureCreatedDirectory(path string) error {
	return restrictToCurrentUser(path, true)
}

func secureFile(path string) error {
	return restrictToCurrentUser(path, false)
}

func secureOpenFile(_ *os.File, path string) error {
	return secureFile(path)
}

// restrictToCurrentUser replaces inherited access with a protected DACL that
// grants full control only to the account running the Connector. This is the
// Windows equivalent of the 0700/0600 permissions used on Unix.
func restrictToCurrentUser(path string, directory bool) error {
	token, err := windows.OpenCurrentProcessToken()
	if err != nil {
		return err
	}
	defer token.Close()

	user, err := token.GetTokenUser()
	if err != nil {
		return err
	}
	inheritance := uint32(0)
	if directory {
		inheritance = windows.SUB_CONTAINERS_AND_OBJECTS_INHERIT
	}
	acl, err := windows.ACLFromEntries(
		[]windows.EXPLICIT_ACCESS{{
			AccessPermissions: windows.GENERIC_ALL,
			AccessMode:        windows.GRANT_ACCESS,
			Inheritance:       inheritance,
			Trustee: windows.TRUSTEE{
				TrusteeForm:  windows.TRUSTEE_IS_SID,
				TrusteeType:  windows.TRUSTEE_IS_USER,
				TrusteeValue: windows.TrusteeValueFromSID(user.User.Sid),
			},
		}},
		nil,
	)
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(
		path,
		windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION,
		nil,
		nil,
		acl,
		nil,
	)
}

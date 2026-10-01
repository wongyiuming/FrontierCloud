package store

import (
	"path"
	"strings"
	"unicode/utf8"
)

type GlobalRenameOperation struct {
	Format   string        `json:"format"`
	Version  int           `json:"version"`
	ID       string        `json:"id"`
	MasterID string        `json:"master_id"`
	Old      string        `json:"old_path"`
	New      string        `json:"new_path"`
	State    string        `json:"-"`
	Media    []GlobalMedia `json:"media"`
	Audit    AdminAudit    `json:"audit"`
}

// Directory rename keeps the parent/category unchanged. Both the HTTP boundary
// and repository use this contract; a journal is never an authorization bypass.
func ValidDirectoryRename(old, target string) bool {
	valid := func(name string) bool {
		parts := strings.Split(name, "/")
		if name != path.Clean(name) || !utf8.ValidString(name) || utf8.RuneCountInString(name) > 1024 || len(parts) < 2 || len(parts) > 3 || parts[0] != "music" && parts[0] != "vido" {
			return false
		}
		for _, part := range parts {
			if part == "" || strings.HasPrefix(part, ".") || strings.ContainsAny(part, "\\:\x00") || len(part) > 255 {
				return false
			}
		}
		return true
	}
	return valid(old) && valid(target) && old != target && path.Dir(old) == path.Dir(target)
}

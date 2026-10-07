package updater

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestFailedRestartRecoversJournalButPreservesExplicitOpenWithoutJournal(t *testing.T) {
	for _, hasJournal := range []bool{false, true} {
		t.Run(map[bool]string{false: "no-journal", true: "journal"}[hasJournal], func(t *testing.T) {
			x, f, status, target := executorFixture(t, "")
			status.State, status.Phase = "failed", "failed"
			s, err := x.private()
			if err != nil {
				t.Fatal(err)
			}
			defer s.Close()
			if hasJournal {
				j := replacement{Target: target, Old: status.CurrentSHA, Snapshots: map[string]Container{}}
				f.mu.Lock()
				for _, c := range f.containers {
					j.Snapshots[c.label("com.docker.compose.service")] = c
				}
				f.mu.Unlock()
				if err = s.write("replacement.json", j, 16<<20); err != nil {
					t.Fatal(err)
				}
			}
			if err = x.Recover(context.Background(), status); err != nil {
				t.Fatal(err)
			}
			_, err = os.Stat(filepath.Join(x.DataDirectory, ".frontiercloud-force-open"))
			if hasJournal && !os.IsNotExist(err) {
				t.Fatal("recovery did not close override", err)
			}
			if !hasJournal && err != nil {
				t.Fatal("restart erased explicit Admin reopening", err)
			}
			var j replacement
			if err = s.read("replacement.json", 16<<20, &j); !os.IsNotExist(err) {
				t.Fatal("completed recovery journal retained", err)
			}
		})
	}
}
func TestOverrideUnknownBytesAreNotErasedByUpdater(t *testing.T) {
	x, _, _, _ := executorFixture(t, "")
	name := filepath.Join(x.DataDirectory, ".frontiercloud-force-open")
	os.WriteFile(name, []byte("unknown"), 0600)
	if err := x.clearForceOpen(context.Background()); err == nil {
		t.Fatal("unknown override erased")
	}
	raw, _ := os.ReadFile(name)
	if string(raw) != "unknown" {
		t.Fatal("unknown bytes changed")
	}
}

package media

import (
	"fmt"
	"strings"
	"testing"
)

func TestLRCParsingContract(t *testing.T) {
	entries, err := ParseLRC([]byte("\ufeff[offset:-100]\n[00:01.25][00:02:003]音乐\n[00:01.25]音乐\n[00:00.02]开始\n[ar:artist]\n[00:03.001] 结束 "))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 4 || entries[0].Time != 0 || entries[0].Text != "开始" || entries[1].Time != 1.15 || entries[2].Time != 1.903 || entries[3].Time != 2.901 {
		t.Fatalf("entries: %+v", entries)
	}
	for _, value := range [][]byte{nil, []byte("[00:01]"), []byte("[00:60]bad"), {0xff}, []byte("[00:01]bad\x00"), []byte("[00:01]" + strings.Repeat("乐", 4001))} {
		if _, err := ParseLRC(value); err == nil {
			t.Errorf("invalid LRC accepted: %.40q", value)
		}
	}
	var many strings.Builder
	for i := 0; i < 10001; i++ {
		fmt.Fprintf(&many, "[00:01]%d\n", i)
	}
	if _, err := ParseLRC([]byte(many.String())); err == nil {
		t.Fatal("oversize lyric accepted")
	}
}

func TestPathQuotingMatchesPythonAndPreservesPlusInQuery(t *testing.T) {
	if got := QuotePath("music/音乐/a+b @&=.mp3"); got != "music/%E9%9F%B3%E4%B9%90/a%2Bb%20%40%26%3D.mp3" {
		t.Fatalf("quote: %s", got)
	}
}

package karaoke

import (
	"context"
	"strings"
	"testing"
)

func TestPythonScryptCompatibilityAndUnicodePolicies(t *testing.T) {
	ctx := context.Background()
	encoded := "scrypt$16384$8$1$AAECAwQFBgcICQoLDA0ODw$p0WV6w7q-mw3T89Pzjf_dDiuSCAITL23FZtwrWwgWq0"
	valid, err := VerifyPassword(ctx, "兼容Password@123", encoded)
	if err != nil || !valid {
		t.Fatal("Python scrypt mismatch", valid, err)
	}
	valid, err = VerifyPassword(ctx, "wrong", encoded)
	if err != nil || valid {
		t.Fatal(valid, err)
	}
	for _, v := range []string{strings.Replace(encoded, "16384", "32768", 1), strings.Replace(encoded, "$8$", "$32$", 1), "scrypt$16384$8$1$malformed$malformed", encoded + "$extra", strings.Repeat("x", 257)} {
		if valid, err := VerifyPassword(ctx, "兼容Password@123", v); err != nil || valid {
			t.Fatal("unsafe hash accepted", valid, err)
		}
	}
	for value, key := range map[string]string{" Ｆｏｏ＿账号 ": "foo_账号", "Straße用户": "strasse用户", "用户名１２３": "用户名123", "abc½": "abc1⁄2"} {
		_, got, err := NormalizeUsername(value)
		if value == "abc½" {
			if err == nil {
				t.Fatal("NFKC fraction slash allowed")
			}
			continue
		}
		if err != nil || got != key {
			t.Fatal(value, got, err)
		}
	}
	for _, v := range []string{"ab", "user-name", "user.name", "abc\x00", "user̈_mark"} {
		if _, _, err := NormalizeUsername(v); err == nil {
			t.Fatal("invalid username accepted", v)
		}
	}
	for _, p := range []string{"Huawei@123", "兼容Password@１２３"} {
		if err := ValidatePassword(p); err != nil {
			t.Fatal(p, err)
		}
	}
	for _, p := range []string{"short", "PASSWORD@123", "password@123", "Password123", "Password@xx", strings.Repeat("A", 129)} {
		if ValidatePassword(p) == nil {
			t.Fatal("weak password accepted", p)
		}
	}
	hash, err := HashPassword(ctx, "Huawei@123")
	if err != nil {
		t.Fatal(err)
	}
	if valid, err := VerifyPassword(ctx, "Huawei@123", hash); err != nil || !valid {
		t.Fatal(valid, err)
	}
	canceled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := HashPassword(canceled, "Huawei@123"); err == nil {
		t.Fatal("canceled KDF continued")
	}
}

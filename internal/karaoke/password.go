package karaoke

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"errors"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"golang.org/x/crypto/scrypt"
	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

var passwordSlots = make(chan struct{}, 4)

func pythonStrip(v string) string {
	return strings.TrimFunc(v, func(r rune) bool { return unicode.IsSpace(r) || r >= 28 && r <= 31 })
}
func NormalizeUsername(value string) (string, string, error) {
	if !utf8.ValidString(value) {
		return "", "", errors.New("用户名需为 3–32 位中文、字母、数字或下划线")
	}
	name := pythonStrip(norm.NFKC.String(value))
	if n := utf8.RuneCountInString(name); n < 3 || n > 32 {
		return "", "", errors.New("用户名需为 3–32 位中文、字母、数字或下划线")
	}
	for _, r := range name {
		if r != '_' && !unicode.IsLetter(r) && !unicode.IsNumber(r) && !(r >= 0x3400 && r <= 0x9fff) {
			return "", "", errors.New("用户名需为 3–32 位中文、字母、数字或下划线")
		}
	}
	return name, cases.Fold().String(name), nil
}
func ValidatePassword(value string) error {
	n := utf8.RuneCountInString(value)
	if !utf8.ValidString(value) || n < 10 || n > 128 {
		return errors.New("密码需为 10–128 个字符")
	}
	var upper, lower, digit, special bool
	for _, r := range value {
		upper = upper || r >= 'A' && r <= 'Z'
		lower = lower || r >= 'a' && r <= 'z'
		digit = digit || unicode.Is(unicode.Nd, r)
		special = special || !(r >= 'A' && r <= 'Z' || r >= 'a' && r <= 'z' || r >= '0' && r <= '9')
	}
	if !upper || !lower || !digit || !special {
		return errors.New("密码必须同时包含大写字母、小写字母、数字和特殊字符")
	}
	return nil
}
func passwordKey(ctx context.Context, password string, salt []byte) ([]byte, error) {
	select {
	case passwordSlots <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	defer func() { <-passwordSlots }()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	key, err := scrypt.Key([]byte(password), salt, 16384, 8, 1, 32)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return key, nil
}
func HashPassword(ctx context.Context, password string) (string, error) {
	if err := ValidatePassword(password); err != nil {
		return "", err
	}
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return "", err
	}
	key, err := passwordKey(ctx, password, salt)
	if err != nil {
		return "", err
	}
	return "scrypt$16384$8$1$" + protocol.Encode(salt) + "$" + protocol.Encode(key), nil
}
func VerifyPassword(ctx context.Context, password, encoded string) (bool, error) {
	if len(encoded) > 256 || !utf8.ValidString(password) || utf8.RuneCountInString(password) > 128 {
		return false, nil
	}
	parts := strings.Split(encoded, "$")
	if len(parts) != 6 || parts[0] != "scrypt" || parts[1] != "16384" || parts[2] != "8" || parts[3] != "1" {
		return false, nil
	}
	salt, err := protocol.Decode(parts[4])
	if err != nil || len(salt) != 16 {
		return false, nil
	}
	want, err := protocol.Decode(parts[5])
	if err != nil || len(want) != 32 {
		return false, nil
	}
	actual, err := passwordKey(ctx, password, salt)
	if err != nil {
		return false, err
	}
	return subtle.ConstantTimeCompare(actual, want) == 1, nil
}

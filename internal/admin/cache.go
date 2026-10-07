package admin

import (
	"context"
	"errors"
	"time"

	"github.com/redis/go-redis/v9"
)

// Cache contains transient sessions and single-use credentials only. The key
// file and SQL audit remain authoritative. Redis failure never bypasses auth.
type Cache interface {
	ReserveAttempt(context.Context, string, int) (int64, error)
	Delete(context.Context, string) error
	TakeTemporary(context.Context, string) (string, error)
	PutTemporary(context.Context, string, string, int) (bool, error)
	SaveSession(context.Context, string, map[string]string, int) error
	Session(context.Context, string) (map[string]string, error)
	Refresh(context.Context, string, int) (bool, error)
	RotationLock(context.Context, string, int) (bool, error)
	RotationUnlock(context.Context, string) error
	ReplaceSessions(context.Context, string, string, int) error
}

type RedisCache struct{ client *redis.Client }

func NewRedisCache(client *redis.Client) *RedisCache { return &RedisCache{client} }

var attemptScript = redis.NewScript(`local count=redis.call('INCR',KEYS[1]); if redis.call('TTL',KEYS[1])<0 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return count`)
var takeScript = redis.NewScript(`local value=redis.call('GET',KEYS[1]); if value then redis.call('DEL',KEYS[1]) end; return value`)
var unlockScript = redis.NewScript(`if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end; return 0`)

func (c *RedisCache) ReserveAttempt(ctx context.Context, key string, window int) (int64, error) {
	return attemptScript.Run(ctx, c.client, []string{key}, window).Int64()
}
func (c *RedisCache) Delete(ctx context.Context, key string) error {
	return c.client.Del(ctx, key).Err()
}
func (c *RedisCache) TakeTemporary(ctx context.Context, key string) (string, error) {
	value, err := takeScript.Run(ctx, c.client, []string{key}).Text()
	if errors.Is(err, redis.Nil) {
		return "", nil
	}
	return value, err
}
func (c *RedisCache) PutTemporary(ctx context.Context, key, value string, ttl int) (bool, error) {
	return c.client.SetNX(ctx, key, value, time.Duration(ttl)*time.Second).Result()
}
func (c *RedisCache) SaveSession(ctx context.Context, key string, values map[string]string, ttl int) error {
	pipe := c.client.TxPipeline()
	pipe.HSet(ctx, key, values)
	expires := pipe.Expire(ctx, key, time.Duration(ttl)*time.Second)
	_, err := pipe.Exec(ctx)
	if err != nil {
		return err
	}
	if !expires.Val() {
		return errors.New("session TTL was not persisted")
	}
	return nil
}
func (c *RedisCache) Session(ctx context.Context, key string) (map[string]string, error) {
	return c.client.HGetAll(ctx, key).Result()
}
func (c *RedisCache) Refresh(ctx context.Context, key string, ttl int) (bool, error) {
	return c.client.Expire(ctx, key, time.Duration(ttl)*time.Second).Result()
}
func (c *RedisCache) RotationLock(ctx context.Context, token string, ttl int) (bool, error) {
	return c.client.SetNX(ctx, "admin:key:rotation-lock", token, time.Duration(ttl)*time.Second).Result()
}
func (c *RedisCache) RotationUnlock(ctx context.Context, token string) error {
	return unlockScript.Run(ctx, c.client, []string{"admin:key:rotation-lock"}, token).Err()
}
func (c *RedisCache) ReplaceSessions(ctx context.Context, current, keyHash string, ttl int) error {
	pipe := c.client.TxPipeline()
	for _, pattern := range []string{"admin:session:*", "admin:temporary-key:*"} {
		iter := c.client.Scan(ctx, 0, pattern, 200).Iterator()
		for iter.Next(ctx) {
			if iter.Val() != current {
				pipe.Unlink(ctx, iter.Val())
			}
		}
		if err := iter.Err(); err != nil {
			return err
		}
	}
	pipe.HSet(ctx, current, "key_hash", keyHash)
	expires := pipe.Expire(ctx, current, time.Duration(ttl)*time.Second)
	_, err := pipe.Exec(ctx)
	if err != nil {
		return err
	}
	if !expires.Val() {
		return errors.New("current session TTL not persisted")
	}
	return nil
}

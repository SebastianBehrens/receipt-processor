# Authelia Integration Guide

This Django application uses Authelia's header-based authentication for seamless integration with your authentication proxy.

## How It Works

1. **Header-Based Authentication**: Authelia sets HTTP headers (`Remote-User`, `Remote-Name`, `Remote-Email`, `Remote-Groups`) that Django reads to authenticate users.

2. **Automatic User Creation**: When a user authenticates through Authelia for the first time, Django automatically creates a user account.

3. **User Data Sync**: User information (name, email, groups) is synchronized from Authelia headers on each request.

## Authelia Configuration

Your Authelia client configuration should look like this:

```yaml
identity_providers:
  oidc:
    clients:
      - client_id: 'receipt-processor'
        client_name: 'Receipt Processor'
        client_secret: 'your-secret-here'
        public: false
        authorization_policy: 'two_factor'
        # ... other OIDC settings not needed for header-based auth
```

## Proxy Configuration

Configure your reverse proxy (nginx, Traefik, etc.) to:

1. **Forward Authentication Headers**: Ensure Authelia's headers are passed to Django:
   - `Remote-User` (username)
   - `Remote-Name` (display name)  
   - `Remote-Email` (email address)
   - `Remote-Groups` (comma-separated list of groups)

2. **Protect All Endpoints**: Every request to Django should be authenticated through Authelia.

### Example Nginx Configuration

```nginx
location / {
    # Forward auth to Authelia
    auth_request /auth;
    
    # Pass authentication headers
    auth_request_set $remote_user $upstream_http_remote_user;
    auth_request_set $remote_name $upstream_http_remote_name;
    auth_request_set $remote_email $upstream_http_remote_email;
    auth_request_set $remote_groups $upstream_http_remote_groups;
    
    # Set headers for Django
    proxy_set_header Remote-User $remote_user;
    proxy_set_header Remote-Name $remote_name;
    proxy_set_header Remote-Email $remote_email;
    proxy_set_header Remote-Groups $remote_groups;
    
    # Forward to Django
    proxy_pass http://django-backend;
}

location /auth {
    internal;
    proxy_pass http://authelia:9091/api/verify;
    proxy_set_header X-Original-URL $scheme://$http_host$request_uri;
}
```

## Django Settings

The following settings control the integration:

```python
# Logout redirect (optional)
AUTHELIA_LOGOUT_URL = 'https://auth.your-domain.com/logout'

# Groups to exclude from synchronization (optional)
AUTHELIA_EXCLUDED_GROUPS = ['system', 'internal']
```

## Security Considerations

⚠️ **Important**: The reverse proxy MUST set the authentication headers on EVERY request to Django. If you only protect certain URLs, make sure to set empty headers (`''`) for unprotected paths to prevent session hijacking.

## Benefits Over OIDC

1. **Simpler Setup**: No client secrets, token endpoints, or complex OIDC flows
2. **More Reliable**: No token expiration or refresh issues  
3. **Better Performance**: No additional HTTP requests for token validation
4. **Standard Approach**: Uses Django's built-in `RemoteUserBackend`
5. **Real-time Sync**: User data updates on every request

## Testing

To test the integration locally without Authelia:

1. Set the `Remote-User` header manually:
   ```bash
   curl -H "Remote-User: testuser" http://localhost:8000/
   ```

2. Or use a development proxy that sets the headers for testing. 
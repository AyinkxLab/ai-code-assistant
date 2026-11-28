# API Examples

This document provides realistic request and response examples for the public API endpoints.

## Authentication

All authenticated endpoints require a Bearer token.

**Request:**
```http
GET /api/v1/health
Authorization: Bearer your-token-here
```

**Response (200):**
```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

## Error responses

**401 Unauthorized:**
```json
{
  "error": "invalid_token",
  "message": "The provided token is invalid or expired"
}
```

**403 Forbidden:**
```json
{
  "error": "insufficient_permissions",
  "message": "You do not have permission to perform this action"
}
```

**404 Not Found:**
```json
{
  "error": "not_found",
  "message": "The requested resource was not found"
}
```

## Pagination example

**Request:**
```http
GET /api/v1/items?page=2&per_page=20
```

**Response:**
```json
{
  "items": [],
  "page": 2,
  "per_page": 20,
  "total": 0
}
```

<?php
/*
 * Loopback-only proxy for the CA Workshop Python service.
 * The service binds to 127.0.0.1 so it is never exposed directly on the LAN.
 */
$path = isset($_GET['path']) ? (string) $_GET['path'] : '';
if (($_SERVER['HTTP_X_CA_WORKSHOP'] ?? '') !== '1') {
    http_response_code(403);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'CA Workshop requests must come from the plugin page.']);
    exit;
}

if (isset($_GET['get_csrf_token']) || isset($_GET['csrf']) || $path === '/api/auth/csrf' || $path === '/api/csrf-token') {
    $token = '';
    global $var;
    if (isset($var['csrf_token']) && is_string($var['csrf_token']) && $var['csrf_token'] !== '') {
        $token = $var['csrf_token'];
    }
    if ($token === '' && file_exists('/var/local/emhttp/var.ini')) {
        $ini = @parse_ini_file('/var/local/emhttp/var.ini');
        if (isset($ini['csrf_token']) && is_string($ini['csrf_token'])) {
            $token = $ini['csrf_token'];
        }
    }
    if ($token === '' && file_exists('/var/local/emhttp/var.ini')) {
        $raw = @file_get_contents('/var/local/emhttp/var.ini');
        if ($raw && preg_match('/csrf_token="([^"]+)"/', $raw, $m)) {
            $token = $m[1];
        }
    }
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo json_encode(['csrf_token' => $token]);
    exit;
}
if ($path === '' || strpos($path, '/api/') !== 0 || strpos($path, '..') !== false || preg_match('/[\r\n]/', $path)) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Invalid CA Workshop API path.']);
    exit;
}

$url = 'http://127.0.0.1:8765' . $path;
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$headers = ['Accept: application/json'];

// If payload was posted via form-urlencoded (to satisfy Unraid local_prepend.php csrf check),
// unpack it as application/json for the Python backend.
$post_payload = null;
if (isset($_POST['payload'])) {
    $post_payload = (string) $_POST['payload'];
    $headers[] = 'Content-Type: application/json';
} elseif (!empty($_SERVER['CONTENT_TYPE'])) {
    $headers[] = 'Content-Type: ' . $_SERVER['CONTENT_TYPE'];
}

$handle = curl_init($url);
curl_setopt_array($handle, [
    CURLOPT_CUSTOMREQUEST => $method,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_FOLLOWLOCATION => false,
    CURLOPT_CONNECTTIMEOUT => 5,
    CURLOPT_TIMEOUT => 180,
    CURLOPT_HTTPHEADER => $headers,
]);
if (in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'], true)) {
    $body = $post_payload !== null ? $post_payload : file_get_contents('php://input');
    curl_setopt($handle, CURLOPT_POSTFIELDS, $body);
}

$body = curl_exec($handle);
$status = (int) (curl_getinfo($handle, CURLINFO_RESPONSE_CODE) ?: 502);
$contentType = curl_getinfo($handle, CURLINFO_CONTENT_TYPE) ?: 'application/json';
$error = curl_error($handle);
curl_close($handle);

if ($body === false) {
    $status = 502;
    $contentType = 'application/json';
    $body = json_encode(['error' => 'CA Workshop is not running on this server.', 'detail' => $error]);
}

http_response_code($status);
header('Content-Type: ' . $contentType);
header('Cache-Control: no-store');
echo $body;

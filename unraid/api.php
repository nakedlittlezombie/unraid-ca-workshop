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
if ($path === '' || strpos($path, '/api/') !== 0 || strpos($path, '..') !== false || preg_match('/[\r\n]/', $path)) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Invalid CA Workshop API path.']);
    exit;
}

$url = 'http://127.0.0.1:8765' . $path;
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$headers = ['Accept: application/json'];
if (!empty($_SERVER['CONTENT_TYPE'])) {
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
    curl_setopt($handle, CURLOPT_POSTFIELDS, file_get_contents('php://input'));
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

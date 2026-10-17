<?php

$base = 'http://localhost:8080/api';

function call(string $method, string $path, ?array $body = null): array
{
    global $base;
    $curl = curl_init($base . $path);
    curl_setopt_array($curl, [
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 30,
        CURLOPT_USERPWD => getenv('ADMIN_EMAIL') . ':' . getenv('ADMIN_PASSWORD'),
        CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
    ]);
    if ($body !== null) {
        curl_setopt($curl, CURLOPT_POSTFIELDS, json_encode($body));
    }
    $response = curl_exec($curl);
    $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    curl_close($curl);

    return [$status, json_decode((string) $response, true) ?? []];
}

function find(string $collection, string $field, string $value): ?array
{
    [$status, $found] = call('GET', "/$collection?limit=100&search=" . urlencode($value));
    if ($status !== 200) {
        fwrite(STDERR, "looking up $collection failed: HTTP $status\n");
        exit(1);
    }
    foreach ($found[$collection] ?? [] as $item) {
        if (($item[$field] ?? null) === $value) {
            return $item;
        }
    }

    return null;
}

function create(string $collection, string $label, array $body): array
{
    [$status, $created] = call('POST', "/$collection/new", $body);
    $item = $created[rtrim($collection, 's')] ?? null;
    if ($status !== 201 || $item === null) {
        fwrite(STDERR, "creating $label failed: HTTP $status\n");
        exit(1);
    }
    echo "created $label\n";

    return $item;
}

while (true) {
    [$status] = call('GET', '/roles?limit=1');
    if ($status === 200) {
        break;
    }
    if ($status === 401 || $status === 403) {
        fwrite(STDERR, "the admin's login is refused: HTTP $status\n");
        exit(1);
    }
    sleep(5);
}

$role = find('roles', 'name', 'Payload') ?? create('roles', 'role Payload', [
    'name' => 'Payload',
    'description' => 'Payload CMS: its contacts and the segments they join',
    'isAdmin' => false,
    'rawPermissions' => [
        'lead:leads' => ['viewother', 'create', 'editother'],
        'lead:lists' => ['viewother', 'editother'],
    ],
]);

find('users', 'username', 'payload') ?? create('users', 'user payload', [
    'username' => 'payload',
    'firstName' => 'Payload',
    'lastName' => 'CMS',
    'email' => getenv('PAYLOAD_API_EMAIL'),
    'role' => $role['id'],
    'plainPassword' => [
        'password' => getenv('PAYLOAD_API_PASSWORD'),
        'confirm' => getenv('PAYLOAD_API_PASSWORD'),
    ],
]);

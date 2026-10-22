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

function ensureUser(string $username, string $firstName, string $lastName, string $email, string $password, string $roleName, string $description, array $permissions): void
{
    $role = find('roles', 'name', $roleName) ?? create('roles', "role $roleName", [
        'name' => $roleName,
        'description' => $description,
        'isAdmin' => false,
        'rawPermissions' => $permissions,
    ]);

    find('users', 'username', $username) ?? create('users', "user $username", [
        'username' => $username,
        'firstName' => $firstName,
        'lastName' => $lastName,
        'email' => $email,
        'role' => $role['id'],
        'plainPassword' => ['password' => $password, 'confirm' => $password],
    ]);
}

$contactsAndSegments = [
    'lead:leads' => ['viewother', 'create', 'editother'],
    'lead:lists' => ['viewother', 'editother'],
];

ensureUser(
    'payload',
    'Payload',
    'CMS',
    getenv('PAYLOAD_API_EMAIL'),
    getenv('PAYLOAD_API_PASSWORD'),
    'Payload',
    'Payload CMS: its contacts and the segments they join',
    $contactsAndSegments,
);

// A file, not an environment variable: the kubelet fills an optional secret
// volume once the operator writes the secret, with no restart of the pod.
$cmsPasswordFile = '/secrets/cms/password';
if (!is_file($cmsPasswordFile)) {
    echo "the cms password is not written yet, waiting for it\n";
    while (!is_file($cmsPasswordFile)) {
        sleep(30);
    }
}

ensureUser(
    'cms',
    'Strapi',
    'CMS',
    getenv('CMS_API_EMAIL'),
    trim((string) file_get_contents($cmsPasswordFile)),
    'CMS',
    "The site's Strapi: its contacts and the segments they join",
    $contactsAndSegments,
);

<?php
declare(strict_types=1);

/**
 * Kolejka MQTT: harmonogram BESS (24 rejestry godzinowe x 4 kwadranse).
 *
 * Zawsze wysyla 96 wartosci power_prc (H00..H23).
 * Dla dzisiejszej doby wysyla rolling 24h od biezacego kwadransa.
 * Dla innej doby wysyla pelne 24h od 00:00.
 * Brakujace kwadranse = 0%.
 *
 * CLI: php limiterBESS_send_schedule.php [YYYY-MM-DD]
 *   - domyslnie dzisiejsza doba (Europe/Warsaw).
 */

require 'config_db.php';

if (!isset($pdo) || !($pdo instanceof PDO)) {
    die("Brak PDO\n");
}

date_default_timezone_set('Europe/Warsaw');

$doba = date('Y-m-d');

if (
    PHP_SAPI === 'cli'
    && isset($argv[1])
    && preg_match('/^\d{4}-\d{2}-\d{2}$/', $argv[1])
) {
    $doba = $argv[1];
}

$logPrefix = '[' . date('Y-m-d H:i:s') . '] ';

function bess_floor_quarter(DateTimeImmutable $dt): DateTimeImmutable
{
    $min = (int) $dt->format('i');
    $q = (int) (floor($min / 15) * 15);

    return $dt->setTime((int) $dt->format('G'), $q, 0);
}

function bess_mqtt_quarter_byte(int $powerPrc): int
{
    if ($powerPrc > 127) {
        return 127;
    }
    if ($powerPrc < -128) {
        return -128;
    }

    return $powerPrc & 0xFF;
}

function buildHourRegister(int $percent): int
{
    $byte = bess_mqtt_quarter_byte($percent);

    return ($byte << 24)
        | ($byte << 16)
        | ($byte << 8)
        | $byte;
}

function buildHourRegisterFromQuarters(
    int $q0,
    int $q1,
    int $q2,
    int $q3
): int {
    $b0 = bess_mqtt_quarter_byte($q0);
    $b1 = bess_mqtt_quarter_byte($q1);
    $b2 = bess_mqtt_quarter_byte($q2);
    $b3 = bess_mqtt_quarter_byte($q3);

    return ($b0 << 24)
        | ($b1 << 16)
        | ($b2 << 8)
        | $b3;
}

/**
 * Indeks kwadransu 0..95 wedlug godziny kalendarzowej.
 * 00:00 = 0, 00:15 = 1, ..., 23:45 = 95.
 */
function bess_calendar_quarter_index(DateTimeImmutable $slotStart): ?int
{
    $hour = (int) $slotStart->format('G');
    $minute = (int) $slotStart->format('i');
    if (!in_array($minute, [0, 15, 30, 45], true)) {
        return null;
    }

    $idx = ($hour * 4) + (int) ($minute / 15);
    if ($idx < 0 || $idx > 95) {
        return null;
    }

    return $idx;
}

/**
 * Zakres wysylki:
 * - dzisiaj: od biezacego kwadransa do +24h,
 * - inna data: od 00:00 do +24h.
 *
 * @return array{anchor: DateTimeImmutable, end: DateTimeImmutable}
 */
function bess_schedule_window(string $doba, ?DateTimeImmutable $now = null): array
{
    $tz = new DateTimeZone('Europe/Warsaw');
    $dayStart = new DateTimeImmutable($doba . ' 00:00:00', $tz);
    $now = $now ?? new DateTimeImmutable('now', $tz);

    $anchor = $dayStart;
    if ($doba === $now->format('Y-m-d')) {
        $anchor = bess_floor_quarter($now);
    }

    return [
        'anchor' => $anchor,
        'end' => $anchor->modify('+24 hours'),
    ];
}

/**
 * 96 wartosci power_prc mapowanych na tagi godzinowe BESS:H00..BESS:H23.
 *
 * Zakres zapytania jest rolling 24h, ale indeks docelowy jest kalendarzowy:
 * BESS:H00 zawsze odpowiada godzinie 00:00, BESS:H01 godzinie 01:00 itd.
 *
 * @return array{slots: list<int>, found: int, anchor: string, end: string}
 */
function bess_load_power_prc_slots(
    PDO $pdo,
    int $idInstalacji,
    string $doba,
    ?DateTimeImmutable $now = null
): array {
    $tz = new DateTimeZone('Europe/Warsaw');
    $window = bess_schedule_window($doba, $now);
    $anchor = $window['anchor'];
    $end = $window['end'];

    $slots96 = array_fill(0, 96, 0);

    $sql = '
        SELECT slot_start_time, power_prc
        FROM t_limiter_bess_schedule
        WHERE id_instalacji = :id_instalacji
          AND slot_start_time >= :t_from
          AND slot_start_time < :t_to
        ORDER BY slot_start_time ASC
        LIMIT 96
    ';

    $st = $pdo->prepare($sql);
    $st->execute([
        ':id_instalacji' => $idInstalacji,
        ':t_from' => $anchor->format('Y-m-d H:i:s'),
        ':t_to' => $end->format('Y-m-d H:i:s'),
    ]);

    $rows = $st->fetchAll(PDO::FETCH_ASSOC);
    $found = 0;

    foreach ($rows as $row) {
        $raw = (string) ($row['slot_start_time'] ?? '');
        if ($raw === '') {
            continue;
        }
        $slotDt = DateTimeImmutable::createFromFormat('Y-m-d H:i:s', $raw, $tz);
        if ($slotDt === false) {
            $slotDt = new DateTimeImmutable($raw, $tz);
        }
        $idx = bess_calendar_quarter_index($slotDt);
        if ($idx === null) {
            continue;
        }
        $slots96[$idx] = (int) $row['power_prc'];
        $found++;
    }

    return [
        'slots' => $slots96,
        'found' => $found,
        'anchor' => $anchor->format('Y-m-d H:i:s'),
        'end' => $end->format('Y-m-d H:i:s'),
    ];
}

/**
 * @param list<int> $slots96
 *
 * @return list<array{tag: string, value: int}>
 */
function bess_build_mqtt_registers(array $slots96): array
{
    $mqtt = [];

    for ($hour = 0; $hour < 24; $hour++) {
        $base = $hour * 4;

        $p0 = $slots96[$base];
        $p1 = $slots96[$base + 1];
        $p2 = $slots96[$base + 2];
        $p3 = $slots96[$base + 3];

        $same = ($p0 === $p1) && ($p1 === $p2) && ($p2 === $p3);

        $value = $same
            ? buildHourRegister($p0)
            : buildHourRegisterFromQuarters($p0, $p1, $p2, $p3);

        $mqtt[] = [
            'tag' => sprintf('BESS:H%02d', $hour),
            'value' => $value,
        ];
    }

    return $mqtt;
}

/**
 * Mapa slot_start_time => power_prc (tylko podgląd HTML, rolling okno).
 *
 * @return array<string, int>
 */
function bess_fetch_slot_map_for_display(
    PDO $pdo,
    int $idInstalacji,
    string $anchor,
    string $end
): array {
    $sql = '
        SELECT slot_start_time, power_prc
        FROM t_limiter_bess_schedule
        WHERE id_instalacji = :id_instalacji
          AND slot_start_time >= :t_from
          AND slot_start_time < :t_to
        ORDER BY slot_start_time ASC
    ';

    $st = $pdo->prepare($sql);
    $st->execute([
        ':id_instalacji' => $idInstalacji,
        ':t_from' => $anchor,
        ':t_to' => $end,
    ]);

    /** @var array<string, int> $map */
    $map = [];
    while ($row = $st->fetch(PDO::FETCH_ASSOC)) {
        $key = (string) ($row['slot_start_time'] ?? '');
        if ($key === '') {
            continue;
        }
        $map[$key] = (int) $row['power_prc'];
    }

    return $map;
}

/**
 * Tabela podglądu: od bieżącej godziny kotwicy, 24 h do przodu (96 kwadransów).
 *
 * @param array<string, int> $slotByTime klucz Y-m-d H:i:s
 */
function bess_print_slot_mapping(array $slotByTime, DateTimeImmutable $anchor): void
{
    $hourStart = $anchor->setTime((int) $anchor->format('G'), 0, 0);

    echo "<table>\n";
    echo "  <thead>\n";
    echo "    <tr><th>Godzina</th><th>00</th><th>15</th><th>30</th><th>45</th></tr>\n";
    echo "  </thead>\n";
    echo "  <tbody>\n";

    for ($h = 0; $h < 24; $h++) {
        $rowStart = $hourStart->modify('+' . $h . ' hours');
        $label = $rowStart->format('Y-m-d H:i');

        echo "    <tr>";
        echo "<td>" . htmlspecialchars($label, ENT_QUOTES, 'UTF-8') . "</td>";

        for ($q = 0; $q < 4; $q++) {
            $slotDt = $rowStart->modify('+' . ($q * 15) . ' minutes');
            $key = $slotDt->format('Y-m-d H:i:s');
            $powerPrc = $slotByTime[$key] ?? 0;
            echo bess_slot_cell($powerPrc);
        }

        echo "</tr>\n";
    }

    echo "  </tbody>\n";
    echo "</table>\n";
}

function bess_slot_label(int $powerPrc): string
{
    if ($powerPrc === 0) {
        return 'AUTO';
    }
    if ($powerPrc === 1) {
        return '';
    }

    return ' ' . abs($powerPrc) . '%';
}

function bess_slot_color(int $powerPrc): string
{
    if ($powerPrc === 1) {
        return '';
    }
    if ($powerPrc > 0) {
        return '#198754'; // zielony - ladowanie
    }
    if ($powerPrc < 0) {
        return '#dc3545'; // czerwony - rozladowanie
    }

    return '#ffc107'; // zolty - auto
}

function bess_slot_cell(int $powerPrc): string
{
    $color = bess_slot_color($powerPrc);
    $attrs = '';

    if ($color !== '') {
        $attrs = ' bgcolor="' . htmlspecialchars($color, ENT_QUOTES, 'UTF-8') . '"';
    }
    if ($powerPrc === 1) {
        $attrs .= ' title="stby"';
    }

    return '<td' . $attrs . '>'
        . htmlspecialchars(bess_slot_label($powerPrc), ENT_QUOTES, 'UTF-8')
        . '</td>';
}

$tz = new DateTimeZone('Europe/Warsaw');
$now = new DateTimeImmutable('now', $tz);
$window = bess_schedule_window($doba, $now);
$listAnchor = $window['anchor'];
$listEnd = $window['end'];

$stInst = $pdo->prepare('
    SELECT id_instalacji
    FROM t_limiter_bess_config
    WHERE harmonogram_auto = 1
    ORDER BY id_instalacji
');
$stInst->execute();

$instalacje = $stInst->fetchAll(PDO::FETCH_COLUMN);

if (!$instalacje) {
    echo $logPrefix . "Brak instalacji z t_limiter_bess_config.harmonogram_auto=1\n";
    exit;
}

$stMqtt = $pdo->prepare('
    INSERT INTO t_mqtt_queue
        (id_instalacji, topic, payload, status)
    VALUES
        (?, ?, ?, \'NEW\')
');

$sent = 0;

foreach ($instalacje as $idInstalacji) {
    try {
        $idInstalacji = (int) $idInstalacji;

        echo $logPrefix . 'Instalacja ' . $idInstalacji . "\n";

        $loaded = bess_load_power_prc_slots($pdo, $idInstalacji, $doba, $now);
        $slots = $loaded['slots'];

        echo $logPrefix
            . '  slotow w bazie ('
            . $loaded['anchor']
            . ' - '
            . $loaded['end']
            . '): '
            . $loaded['found']
            . " / 96 kwadransow rolling 24h\n";

        if ($loaded['found'] === 0) {
            echo $logPrefix . '  POMINIETO - brak danych od kotwicy czasu' . "\n";
            continue;
        }

        $anchorDt = DateTimeImmutable::createFromFormat('Y-m-d H:i:s', $loaded['anchor'], $tz);
        if ($anchorDt === false) {
            $anchorDt = new DateTimeImmutable($loaded['anchor'], $tz);
        }

        $slotMapForDisplay = bess_fetch_slot_map_for_display(
            $pdo,
            $idInstalacji,
            $loaded['anchor'],
            $loaded['end']
        );

        echo $logPrefix . "  mapowanie slotow (od biezacej godziny, +24h):\n";
        bess_print_slot_mapping($slotMapForDisplay, $anchorDt);

        $mqttData = bess_build_mqtt_registers($slots);

        $payload = json_encode(['w' => $mqttData], JSON_UNESCAPED_UNICODE);
        if ($payload === false) {
            throw new RuntimeException('json_encode payload MQTT');
        }

        $topic = 'scada/'
            . str_pad((string) $idInstalacji, 6, '0', STR_PAD_LEFT)
            . '/cmd';

        $stMqtt->execute([$idInstalacji, $topic, $payload]);

        echo $logPrefix . 'MQTT => ' . $topic . "\n";

        $sent++;
    } catch (Throwable $e) {
        echo $logPrefix
            . 'BLAD instalacja '
            . $idInstalacji
            . ' : '
            . $e->getMessage()
            . "\n";
    }
}

echo $logPrefix . 'Zakonczono. Wyslano ' . $sent . " harmonogramow.\n";

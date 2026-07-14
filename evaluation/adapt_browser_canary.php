<?php

declare(strict_types=1);

use App\Assignment;
use App\Question;
use App\User;
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Support\Facades\DB;

const BUILD08_BROWSER_MARKER = 'adapt-final-seed-disposable-clone';
const BUILD08_BROWSER_SCHEMA = 'build08-adapt-browser-manifest-v1';
const BUILD08_BROWSER_SOURCE = 'https://chem.libretexts.org/Bookshelves/BUILD08/Assessment_Browser_Qualification';

browserFailUnless(getenv('BUILD08_CANARY_MARKER') === BUILD08_BROWSER_MARKER, 'canary marker is absent');
browserFailUnless(getenv('BUILD08_CANARY_CONFIRM') === BUILD08_BROWSER_MARKER, 'operator confirmation is absent');
browserFailUnless(getenv('APP_URL') === 'http://127.0.0.1:18081', 'APP_URL is not the disposable canary');
browserFailUnless(getenv('HINTING_V2_MODE') === 'off', 'hint mode is not off');
browserFailUnless(getenv('HINTING_V2_STAFF_PREVIEW') === 'false', 'hint staff preview is not false');
browserFailUnless(getenv('HINTING_V2_MASTERY_ENABLED') === 'false', 'hint mastery is not false');
browserFailUnless(getenv('BUILD08_CANARY_NETWORK_INTERNAL') === 'true', 'internal-network attestation is absent');

$imageSha = browserDigest('BUILD08_CANARY_IMAGE_SHA256', true);
$backupSha = browserDigest('BUILD08_CLONE_BACKUP_SHA256', false);
$inputPath = browserReadablePath('BUILD08_ADAPT_BROWSER_INPUT');
$outputPath = browserOutputPath('BUILD08_ADAPT_BROWSER_OUTPUT');

require '/var/www/tmp/vendor/autoload.php';
$app = require '/var/www/tmp/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();

browserFailUnless((string)config('hinting.mode', 'off') === 'off', 'runtime hint config is not off');
$manifest = json_decode((string)file_get_contents($inputPath), true, 512, JSON_THROW_ON_ERROR);
browserFailUnless(is_array($manifest), 'browser manifest is not an object');
browserFailUnless(($manifest['schema_version'] ?? null) === BUILD08_BROWSER_SCHEMA, 'browser manifest schema changed');
browserFailUnless(($manifest['canary_marker'] ?? null) === BUILD08_BROWSER_MARKER, 'browser manifest marker changed');
browserFailUnless(($manifest['item_type_count'] ?? null) === 19, 'browser manifest does not contain 19 item types');
browserFailUnless(($manifest['native_qti_count'] ?? null) === 17, 'browser manifest does not contain 17 native QTI items');
browserFailUnless(($manifest['external_engine_count'] ?? null) === 2, 'browser manifest does not contain two engine items');
$items = $manifest['items'] ?? null;
browserFailUnless(is_array($items) && count($items) === 19, 'browser manifest items are incomplete');
$canonical = json_encode($items, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
browserFailUnless(hash('sha256', $canonical) === ($manifest['items_sha256'] ?? null), 'browser manifest digest changed');

$instructorEmail = getenv('BUILD08_INSTRUCTOR_EMAIL') ?: 'johnny@libretexts.dev';
$studentEmail = getenv('BUILD08_STUDENT_EMAIL') ?: 'student@libretexts.dev';
$instructor = User::where('email', $instructorEmail)->where('role', 2)->firstOrFail();
$student = User::where('email', $studentEmail)->where('role', 3)->firstOrFail();
$service = User::where('role', 5)->orderBy('id')->firstOrFail();
$serviceFolder = DB::table('saved_questions_folders')
    ->where('user_id', $service->id)
    ->where('type', 'my_questions')
    ->first();
browserFailUnless($serviceFolder !== null, 'service-owned question folder is missing');

$courseId = DB::table('courses')
    ->join('enrollments', 'enrollments.course_id', '=', 'courses.id')
    ->where('courses.user_id', $instructor->id)
    ->where('enrollments.user_id', $student->id)
    ->orderBy('courses.id')
    ->value('courses.id');
browserFailUnless(is_numeric($courseId), 'instructor/student canary course is missing');
$template = Assignment::where('course_id', (int)$courseId)->orderBy('id')->firstOrFail();
$assignmentName = 'BUILD-08 ADAPT browser matrix';
$assignment = Assignment::where('course_id', (int)$courseId)->where('name', $assignmentName)->first();
if ($assignment === null) {
    $assignment = $template->replicate();
    $assignment->name = $assignmentName;
    $assignment->formative = 1;
    $assignment->assessment_type = 'delayed';
    $assignment->number_of_allowed_attempts = 'unlimited';
    $assignment->number_of_allowed_attempts_penalty = 0;
    $assignment->scoring_type = 'p';
    $assignment->points_per_question = 'number of points';
    $assignment->default_points_per_question = 1;
    $assignment->total_points = 19;
    $assignment->late_policy = 'not accepted';
    $assignment->shown = 1;
    $assignment->save();
}
$assignment->shown = 1;
$assignment->save();

DB::transaction(static function () use ($assignment, $student, $courseId): void {
    $timingIds = DB::table('assign_to_timings')->where('assignment_id', $assignment->id)->pluck('id');
    if ($timingIds->isNotEmpty()) {
        DB::table('assign_to_users')->whereIn('assign_to_timing_id', $timingIds)->delete();
        DB::table('assign_to_groups')->whereIn('assign_to_timing_id', $timingIds)->delete();
        DB::table('assign_to_timings')->whereIn('id', $timingIds)->delete();
    }
    $now = now();
    $timingId = DB::table('assign_to_timings')->insertGetId([
        'assignment_id' => $assignment->id,
        'available_from' => $now->copy()->subHour(),
        'due' => $now->copy()->addDay(),
        'final_submission_deadline' => $now->copy()->addDays(2),
        'created_at' => $now,
        'updated_at' => $now,
    ]);
    DB::table('assign_to_groups')->insert([
        'assign_to_timing_id' => $timingId,
        'group' => 'course',
        'group_id' => (int)$courseId,
        'created_at' => $now,
        'updated_at' => $now,
    ]);
    DB::table('assign_to_users')->insert([
        'assign_to_timing_id' => $timingId,
        'user_id' => $student->id,
        'created_at' => $now,
        'updated_at' => $now,
    ]);
});

DB::table('submissions')->where('assignment_id', $assignment->id)->delete();
DB::table('scores')->where('assignment_id', $assignment->id)->delete();
DB::table('data_shops')->where('assignment_id', $assignment->id)->delete();

$records = [];
$seenTypes = [];
foreach ($items as $index => $item) {
    browserFailUnless(is_array($item), 'browser item is not an object');
    $itemType = browserString($item, 'item_type');
    browserFailUnless(!isset($seenTypes[$itemType]), "duplicate browser item type {$itemType}");
    $seenTypes[$itemType] = true;
    $technology = browserString($item, 'technology');
    if ($technology === 'qti') {
        $question = browserEnsureQtiQuestion($item, (int)$service->id, (int)$serviceFolder->id);
    } else {
        browserFailUnless(in_array($technology, ['webwork', 'imathas'], true), "unknown technology {$technology}");
        $question = Question::where('title', browserString($item, 'existing_title'))
            ->where('technology', $technology)
            ->firstOrFail();
    }
    DB::table('assignment_question')->updateOrInsert(
        ['assignment_id' => $assignment->id, 'question_id' => $question->id],
        [
            'open_ended_submission_type' => 'text',
            'open_ended_text_editor' => null,
            'points' => 1,
            'weight' => null,
            'completion_scoring_mode' => null,
            'order' => $index + 1,
            'updated_at' => now(),
        ]
    );
    $records[] = [
        'fixture_id' => browserString($item, 'fixture_id'),
        'item_type' => $itemType,
        'technology' => $technology,
        'qti_type' => $technology === 'qti' ? browserString($item, 'qti_type') : null,
        'question_id' => (int)$question->id,
        'expected_response' => $item['expected_response'] ?? null,
    ];
}
browserFailUnless(count($records) === 19 && count($seenTypes) === 19, 'seeded browser matrix is incomplete');

$output = [
    'schema_version' => 'build08-adapt-browser-canary-v1',
    'canary_marker' => BUILD08_BROWSER_MARKER,
    'adapt_image_sha256' => $imageSha,
    'clone_backup_sha256' => $backupSha,
    'network_internal' => true,
    'hint_mode' => 'off',
    'assignment_id' => (int)$assignment->id,
    'item_type_count' => count($records),
    'items_sha256' => $manifest['items_sha256'],
    'items' => $records,
];
file_put_contents(
    $outputPath,
    json_encode($output, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n",
    LOCK_EX
);
fwrite(STDOUT, json_encode([
    'assignment_id' => (int)$assignment->id,
    'items' => count($records),
    'native_qti' => 17,
    'external_engines' => 2,
    'passed' => true,
], JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n");

function browserEnsureQtiQuestion(array $item, int $serviceId, int $folderId): Question
{
    $fixtureId = browserString($item, 'fixture_id');
    $notes = "BUILD08_BROWSER_KEY:{$fixtureId}";
    $question = Question::where('question_editor_user_id', $serviceId)->where('notes', $notes)->first();
    $qti = $item['qti_json'] ?? null;
    browserFailUnless(is_array($qti), "{$fixtureId} QTI payload is missing");
    $qtiType = browserString($item, 'qti_type');
    browserFailUnless(($qti['questionType'] ?? null) === $qtiType, "{$fixtureId} QTI type changed");
    $encoded = json_encode($qti, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    if ($question === null) {
        $question = new Question();
        $question->question_type = 'assessment';
        $question->page_id = 1;
        $question->library = 'adapt';
        $question->url = null;
        $question->title = 'BUILD-08 browser ' . browserString($item, 'item_type');
        $question->technology = 'qti';
        $question->technology_id = null;
        $question->technology_iframe = '';
        $question->qti_json_type = $qtiType;
        $question->qti_json = $encoded;
        $question->solution_html = '<p>Sealed BUILD-08 browser fixture.</p>';
        $question->notes = $notes;
        $question->author = 'LibreTexts Assessment AI';
        $question->question_editor_user_id = $serviceId;
        $question->license = 'ccby';
        $question->source_url = BUILD08_BROWSER_SOURCE;
        $question->public = 0;
        $question->cached = 0;
        $question->folder_id = $folderId;
        $question->save();
        $question->page_id = $question->id;
        $question->save();
    }
    browserFailUnless($question->technology === 'qti', "{$fixtureId} technology changed");
    browserFailUnless($question->source_url === BUILD08_BROWSER_SOURCE, "{$fixtureId} source changed");
    $question->qti_json_type = $qtiType;
    $question->qti_json = $encoded;
    $question->save();
    return $question->fresh();
}

function browserReadablePath(string $name): string
{
    $value = getenv($name);
    browserFailUnless(is_string($value) && $value !== '' && is_file($value) && is_readable($value), "{$name} is not readable");
    return $value;
}

function browserOutputPath(string $name): string
{
    $value = getenv($name);
    browserFailUnless(is_string($value) && strpos($value, '/tmp/build08-') === 0, "{$name} must be a /tmp/build08-* path");
    return $value;
}

function browserDigest(string $name, bool $withPrefix): string
{
    $value = getenv($name);
    $pattern = $withPrefix ? '/^sha256:[0-9a-f]{64}$/' : '/^[0-9a-f]{64}$/';
    browserFailUnless(is_string($value) && preg_match($pattern, $value) === 1, "{$name} is not a full SHA-256");
    return $value;
}

function browserString(array $record, string $key): string
{
    $value = $record[$key] ?? null;
    browserFailUnless(is_string($value) && $value !== '', "missing string {$key}");
    return $value;
}

function browserFailUnless(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

<?php

declare(strict_types=1);

use App\Assignment;
use App\AssignmentSyncQuestion;
use App\DataShop;
use App\Http\Requests\StoreSubmission;
use App\Question;
use App\Score;
use App\Submission;
use App\User;
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Gate;

const CANARY_MARKER = 'adapt-final-seed-disposable-clone';
const SOURCE_URL = 'https://math.libretexts.org/Bookshelves/BUILD08/Parameterized_Engine_Qualification';

failUnless(getenv('BUILD08_CANARY_MARKER') === CANARY_MARKER, 'canary marker is absent');
failUnless(getenv('BUILD08_CANARY_CONFIRM') === CANARY_MARKER, 'operator confirmation is absent');
failUnless(getenv('APP_URL') === 'http://127.0.0.1:18081', 'APP_URL is not the disposable canary');
failUnless(getenv('HINTING_V2_MODE') === 'off', 'hint mode is not off');
failUnless(getenv('HINTING_V2_STAFF_PREVIEW') === 'false', 'hint staff preview is not false');
failUnless(getenv('HINTING_V2_MASTERY_ENABLED') === 'false', 'hint mastery is not false');
failUnless(getenv('BUILD08_CANARY_NETWORK_INTERNAL') === 'true', 'internal-network attestation is absent');

$imageSha = requiredDigest('BUILD08_CANARY_IMAGE_SHA256', true);
$backupSha = requiredDigest('BUILD08_CLONE_BACKUP_SHA256', false);
$probePath = requiredReadablePath('BUILD08_ENGINE_PROBES');
$itemPath = requiredReadablePath('BUILD08_SEED_ITEMS');
$outputPath = requiredOutputPath('BUILD08_ATTESTATIONS');

require '/var/www/tmp/vendor/autoload.php';
$app = require '/var/www/tmp/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();

failUnless((string)config('hinting.mode', 'off') === 'off', 'runtime hint config is not off');

$items = readJsonl($itemPath, 'seed item');
$probes = readJsonl($probePath, 'engine probe');
failUnless(count($items) === 40, 'seed item manifest does not contain 40 items');
failUnless(count($probes) === 4000, 'engine probe ledger does not contain 4000 rows');

$itemsById = [];
foreach ($items as $item) {
    $itemId = requiredString($item, 'item_id');
    failUnless(!isset($itemsById[$itemId]), "duplicate seed item {$itemId}");
    $source = requiredString($item, 'engine_source');
    failUnless(hash('sha256', $source) === requiredString($item, 'source_sha256'), "source digest mismatch for {$itemId}");
    if (requiredString($item, 'item_type') === 'imathas') {
        $technologyId = requiredPositiveInt($item, 'technology_id');
        failUnless(
            hash('sha256', "imathas-question:{$technologyId}") === requiredString($item, 'engine_object_sha256'),
            "IMathAS object mismatch for {$itemId}"
        );
    }
    $itemsById[$itemId] = $item;
}

$runIds = [];
$probeKeys = [];
foreach ($probes as $probe) {
    $itemId = requiredString($probe, 'item_id');
    $seed = requiredPositiveInt($probe, 'seed');
    $key = "{$itemId}/{$seed}";
    failUnless(isset($itemsById[$itemId]), "probe {$key} has no seed item");
    failUnless(!isset($probeKeys[$key]), "duplicate probe {$key}");
    $probeKeys[$key] = true;
    $runIds[requiredString($probe, 'run_id')] = true;
    failUnless($seed <= 100, "probe {$key} seed is outside 1-100");
    failUnless(requiredBool($probe, 'deterministic'), "probe {$key} is nondeterministic");
    failUnless(requiredBool($probe, 'constraints_satisfied'), "probe {$key} violates constraints");
    failUnless(requiredBool($probe, 'rendered'), "probe {$key} did not render");
    failUnless(requiredInt($probe, 'warning_count') === 0, "probe {$key} has warnings");
    failUnless(requiredInt($probe, 'error_count') === 0, "probe {$key} has errors");
    failUnless(requiredBool($probe, 'expected_answer_accepted'), "probe {$key} rejected the expected answer");
    failUnless(requiredBool($probe, 'wrong_answer_rejected'), "probe {$key} accepted the wrong answer");
    failUnless(abs(requiredFloat($probe, 'expected_score') - 1.0) < 0.000000001, "probe {$key} expected score is not 1");
    failUnless(requiredString($probe, 'source_sha256') === requiredString($itemsById[$itemId], 'source_sha256'), "probe {$key} source changed");
}
failUnless(count($runIds) === 1, 'engine probe ledger contains multiple run IDs');
$runId = array_key_first($runIds);

$service = User::where('role', 5)->firstOrFail();
$student = User::where('role', 3)->orderBy('id')->firstOrFail();
$otherOwner = User::where('email', 'build08-cross-owner@libretexts.dev')->first();
if ($otherOwner === null) {
    $otherOwner = User::where('role', 2)->orderBy('id')->firstOrFail()->replicate();
    $otherOwner->first_name = 'BUILD08';
    $otherOwner->last_name = 'CrossOwner';
    $otherOwner->email = 'build08-cross-owner@libretexts.dev';
    $otherOwner->student_id = null;
    $otherOwner->role = 2;
    $otherOwner->password = null;
    $otherOwner->remember_token = null;
    $otherOwner->save();
}
failUnless(!$otherOwner->isDeveloper(), 'cross-owner identity has developer bypass');
$serviceFolder = DB::table('saved_questions_folders')
    ->where('user_id', $service->id)
    ->where('type', 'my_questions')
    ->first();
$otherFolder = DB::table('saved_questions_folders')
    ->where('user_id', $otherOwner->id)
    ->where('type', 'my_questions')
    ->first();
if ($otherFolder === null) {
    $otherFolderId = DB::table('saved_questions_folders')->insertGetId([
        'user_id' => $otherOwner->id,
        'name' => 'BUILD-08 Cross-Owner Folder',
        'type' => 'my_questions',
        'created_at' => now(),
        'updated_at' => now(),
    ]);
    $otherFolder = DB::table('saved_questions_folders')->where('id', $otherFolderId)->first();
}
failUnless($serviceFolder !== null && $otherFolder !== null, 'owner-scoped folders are missing');

$templateAssignment = Assignment::where('course_id', 1)->orderBy('id')->firstOrFail();
$course = $templateAssignment->course;
$course->formative = 1;
$course->save();
$assignmentName = "BUILD-08 final seed canary {$runId}";
$assignment = Assignment::where('course_id', $course->id)->where('name', $assignmentName)->first();
if ($assignment === null) {
    $assignment = $templateAssignment->replicate();
    $assignment->name = $assignmentName;
    $assignment->formative = 1;
    $assignment->assessment_type = 'delayed';
    $assignment->number_of_allowed_attempts = 'unlimited';
    $assignment->number_of_allowed_attempts_penalty = 0;
    $assignment->scoring_type = 'p';
    $assignment->points_per_question = 'number of points';
    $assignment->default_points_per_question = 1;
    $assignment->total_points = 40;
    $assignment->late_policy = 'not accepted';
    $assignment->shown = 0;
    $assignment->save();
}

// The assignment is canary-owned and hidden. Clearing only its generated rows
// makes an interrupted run safely restartable without touching cloned live data.
DB::table('submissions')->where('assignment_id', $assignment->id)->delete();
DB::table('scores')->where('assignment_id', $assignment->id)->delete();
DB::table('data_shops')->where('assignment_id', $assignment->id)->delete();

$questionState = [];
foreach ($itemsById as $itemId => $item) {
    $first = ensureQuestion($item, (int)$service->id, (int)$serviceFolder->id);
    $second = ensureQuestion($item, (int)$service->id, (int)$serviceFolder->id);
    $publicationKey = publicationKey($item);
    $matchingQuestions = Question::where('question_editor_user_id', $service->id)
        ->where('notes', "BUILD08_PUBLICATION_KEY:{$publicationKey}")
        ->count();
    $idempotent = $first->id === $second->id && $matchingQuestions === 1;
    failUnless($idempotent, "ADAPT question is not idempotent for {$itemId}");

    DB::table('assignment_question')->updateOrInsert(
        ['assignment_id' => $assignment->id, 'question_id' => $first->id],
        [
            'open_ended_submission_type' => 'text',
            'open_ended_text_editor' => null,
            'points' => 1,
            'weight' => null,
            'completion_scoring_mode' => null,
            'order' => count($questionState) + 1,
            'updated_at' => now(),
        ]
    );

    $denial = Gate::forUser($otherOwner)->inspect('update', [$first, (int)$otherFolder->id]);
    failUnless($denial->denied(), "cross-owner update was not denied for {$itemId}");
    $questionState[$itemId] = [
        'question' => $first,
        'idempotent' => $idempotent,
        'cross_owner_denied' => true,
        'count' => 0,
    ];
}

Auth::guard()->setUser($student);
$output = fopen($outputPath, 'wb');
failUnless($output !== false, 'could not open attestation output');

usort($probes, static function (array $left, array $right): int {
    return [requiredString($left, 'item_type'), requiredString($left, 'item_id'), requiredPositiveInt($left, 'seed')]
        <=> [requiredString($right, 'item_type'), requiredString($right, 'item_id'), requiredPositiveInt($right, 'seed')];
});

$written = 0;
foreach ($probes as $probe) {
    $itemId = requiredString($probe, 'item_id');
    $itemType = requiredString($probe, 'item_type');
    $seed = requiredPositiveInt($probe, 'seed');
    $state = &$questionState[$itemId];
    $question = $state['question'];
    $expectedScore = requiredFloat($probe, 'expected_score');
    $submissionPayload = $itemType === 'webwork'
        ? (object)['score' => (object)['result' => $expectedScore], 'seed' => $seed]
        : (object)['score' => $expectedScore, 'seed' => $seed];
    $request = StoreSubmission::create('/api/submissions', 'POST');
    $request->replace([
        'assignment_id' => $assignment->id,
        'question_id' => $question->id,
        'technology' => $itemType,
        'submission' => $submissionPayload,
        'sub_content_id' => null,
    ]);
    $request->setUserResolver(static fn() => $student);
    $response = (new Submission())->store(
        $request,
        new Submission(),
        new Assignment(),
        new Score(),
        new DataShop(),
        new AssignmentSyncQuestion()
    );
    failUnless(($response['type'] ?? null) === 'success', "ADAPT submission failed for {$itemId}/{$seed}");

    $persisted = Submission::where('user_id', $student->id)
        ->where('assignment_id', $assignment->id)
        ->where('question_id', $question->id)
        ->firstOrFail();
    $persisted->refresh();
    $state['count']++;
    $gradeMatches = abs((float)$persisted->score - $expectedScore) < 0.000000001;
    $countMatches = (int)$persisted->submission_count === $state['count'];
    failUnless($gradeMatches && $countMatches, "refreshed grade mismatch for {$itemId}/{$seed}");

    $attestation = [
        'schema_version' => 'build08-adapt-seed-attestation-v1',
        'run_id' => requiredString($probe, 'run_id'),
        'item_id' => $itemId,
        'item_type' => $itemType,
        'seed' => $seed,
        'compiler_version' => requiredString($probe, 'compiler_version'),
        'source_sha256' => requiredString($probe, 'source_sha256'),
        'adapt_image_sha256' => $imageSha,
        'clone_backup_sha256' => $backupSha,
        'adapt_question_id' => (int)$question->id,
        'adapt_assignment_id' => (int)$assignment->id,
        'adapt_submission_id' => (int)$persisted->id,
        'expected_score' => $expectedScore,
        'persisted_score' => (float)$persisted->score,
        'submission_count' => (int)$persisted->submission_count,
        'grade_refreshed' => true,
        'object_idempotent' => (bool)$state['idempotent'],
        'cross_owner_access_blocked' => (bool)$state['cross_owner_denied'],
        'canary_network_internal' => true,
        'hint_mode_off' => true,
    ];
    failUnless(fwrite($output, json_encode($attestation, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n") !== false, 'could not write attestation');
    $written++;
    unset($state);
}
fclose($output);

foreach ($questionState as $itemId => $state) {
    failUnless($state['count'] === 100, "{$itemId} did not persist 100 seeds");
}
failUnless($written === 4000, 'did not write 4000 attestations');

fwrite(STDOUT, json_encode([
    'assignment_id' => (int)$assignment->id,
    'attestations' => $written,
    'items' => count($questionState),
    'passed' => true,
], JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n");

function ensureQuestion(array $item, int $serviceId, int $folderId): Question
{
    $publicationKey = publicationKey($item);
    $notes = "BUILD08_PUBLICATION_KEY:{$publicationKey}";
    $question = Question::where('question_editor_user_id', $serviceId)
        ->where('notes', $notes)
        ->first();
    if ($question !== null) {
        failUnless($question->technology === requiredString($item, 'item_type'), 'existing question technology changed');
        failUnless($question->source_url === SOURCE_URL, 'existing question source URL changed');
        return $question;
    }

    $itemId = requiredString($item, 'item_id');
    $itemType = requiredString($item, 'item_type');
    $question = new Question();
    $question->question_type = 'assessment';
    $question->page_id = 1;
    $question->library = 'adapt';
    $question->url = null;
    $question->title = "BUILD-08 final seed {$itemId}";
    $question->technology = $itemType;
    $question->technology_id = $itemType === 'imathas'
        ? requiredPositiveInt($item, 'technology_id')
        : "build08/{$itemId}.pg";
    $question->technology_iframe = $itemType === 'imathas'
        ? '<iframe class="imathas_problem" src="https://imathas.libretexts.dev/adapt/embedq2.php?id=' . $question->technology_id . '"></iframe>'
        : '';
    $question->webwork_code = $itemType === 'webwork' ? requiredString($item, 'engine_source') : null;
    $question->text_question = '<p>Find the runtime-generated momentum.</p>';
    $question->solution_html = '<p>Multiply mass by speed.</p>';
    $question->notes = $notes;
    $question->author = 'LibreTexts Assessment AI';
    $question->question_editor_user_id = $serviceId;
    $question->license = 'ccby';
    $question->source_url = SOURCE_URL;
    $question->public = 0;
    $question->cached = 0;
    $question->folder_id = $folderId;
    $question->save();
    $question->page_id = $question->id;
    $question->save();
    return $question->fresh();
}

function publicationKey(array $item): string
{
    return hash('sha256', implode(':', [
        requiredString($item, 'run_id'),
        requiredString($item, 'item_id'),
        requiredString($item, 'source_sha256'),
    ]));
}

function readJsonl(string $path, string $label): array
{
    $records = [];
    $handle = fopen($path, 'rb');
    failUnless($handle !== false, "could not open {$label} ledger");
    $lineNumber = 0;
    while (($line = fgets($handle)) !== false) {
        $lineNumber++;
        if (trim($line) === '') {
            continue;
        }
        try {
            $record = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException $exception) {
            throw new RuntimeException("invalid {$label} JSON on line {$lineNumber}", 0, $exception);
        }
        failUnless(is_array($record), "invalid {$label} record on line {$lineNumber}");
        $records[] = $record;
    }
    fclose($handle);
    return $records;
}

function requiredReadablePath(string $name): string
{
    $value = getenv($name);
    failUnless(is_string($value) && $value !== '' && is_file($value) && is_readable($value), "{$name} is not a readable file");
    return $value;
}

function requiredOutputPath(string $name): string
{
    $value = getenv($name);
    failUnless(is_string($value) && strpos($value, '/tmp/build08-') === 0, "{$name} must be a /tmp/build08-* path");
    return $value;
}

function requiredDigest(string $name, bool $withPrefix): string
{
    $value = getenv($name);
    $pattern = $withPrefix ? '/^sha256:[0-9a-f]{64}$/' : '/^[0-9a-f]{64}$/';
    failUnless(is_string($value) && preg_match($pattern, $value) === 1, "{$name} is not a full SHA-256");
    return $value;
}

function requiredString(array $record, string $key): string
{
    $value = $record[$key] ?? null;
    failUnless(is_string($value) && $value !== '', "missing string {$key}");
    return $value;
}

function requiredInt(array $record, string $key): int
{
    $value = $record[$key] ?? null;
    failUnless(is_int($value), "missing integer {$key}");
    return $value;
}

function requiredPositiveInt(array $record, string $key): int
{
    $value = requiredInt($record, $key);
    failUnless($value > 0, "{$key} must be positive");
    return $value;
}

function requiredFloat(array $record, string $key): float
{
    $value = $record[$key] ?? null;
    failUnless(is_int($value) || is_float($value), "missing number {$key}");
    return (float)$value;
}

function requiredBool(array $record, string $key): bool
{
    $value = $record[$key] ?? null;
    failUnless(is_bool($value), "missing boolean {$key}");
    return $value;
}

function failUnless(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

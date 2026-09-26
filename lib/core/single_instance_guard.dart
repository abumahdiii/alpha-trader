import 'dart:ffi' as ffi;
import 'package:ffi/ffi.dart';
import 'package:win32/win32.dart';

import 'app_logger.dart';

/// Refuses to start a second copy of the app. Two instances would each spawn
/// their own Python engine and fight over the same localhost port and the
/// same `data/` files, so the second launch is stopped before anything starts.
///
/// Detection is deliberately based ONLY on Win32 return values, never on
/// `GetLastError()`. `GetLastError()` is thread-local and only valid until the
/// next call that sets it; in a full Flutter app the VM can run its own work
/// between two separate FFI calls, which clobbers the value — and a clobbered
/// read looks exactly like `ERROR_SUCCESS`. `OpenMutexW` answers "does this
/// already exist?" in its return value, so nothing is lost in between.
///
/// `package:win32` exposes no mutex functions, so both are bound here.
typedef _CreateMutexWNative = ffi.IntPtr Function(
  ffi.Pointer<ffi.Void> lpMutexAttributes,
  ffi.Int32 bInitialOwner,
  ffi.Pointer<Utf16> lpName,
);
typedef _CreateMutexWDart = int Function(
  ffi.Pointer<ffi.Void> lpMutexAttributes,
  int bInitialOwner,
  ffi.Pointer<Utf16> lpName,
);

typedef _OpenMutexWNative = ffi.IntPtr Function(
  ffi.Uint32 dwDesiredAccess,
  ffi.Int32 bInheritHandle,
  ffi.Pointer<Utf16> lpName,
);
typedef _OpenMutexWDart = int Function(
  int dwDesiredAccess,
  int bInheritHandle,
  ffi.Pointer<Utf16> lpName,
);

final ffi.DynamicLibrary _kernel32 = ffi.DynamicLibrary.open('kernel32.dll');
final _CreateMutexWDart _createMutexW = _kernel32
    .lookupFunction<_CreateMutexWNative, _CreateMutexWDart>('CreateMutexW');
final _OpenMutexWDart _openMutexW = _kernel32
    .lookupFunction<_OpenMutexWNative, _OpenMutexWDart>('OpenMutexW');

/// Tried in order. `Global\` covers a second copy started from another
/// session (fast user switching / RDP), which matters because the engine's
/// localhost port is machine-wide, not per-session. Creating a `Global\` object
/// needs SeCreateGlobalPrivilege, which an interactive user normally has but
/// a locked-down machine may withhold, so the plain session-scoped name is
/// kept as a fallback rather than losing the guard entirely there.
const List<String> _mutexNames = <String>[
  'Global\\AlphaTrader_SingleInstance_Mutex_v1',
  'AlphaTrader_SingleInstance_Mutex_v1',
];

/// Kept alive for the process lifetime so the kernel object stays alive.
/// Windows destroys it once the last handle closes, which happens
/// automatically on process exit — clean OR crashed — so the lock
/// self-heals and needs no explicit release path.
// ignore: unused_element
int? _mutexHandle;

/// Returns true if this is the only running instance (and holds the lock for
/// the rest of the process lifetime), false if another instance already has it.
bool acquireSingleInstanceLock() {
  for (final String name in _mutexNames) {
    final bool? result = _tryAcquire(name);
    if (result != null) return result;
  }
  // No name could be used at all. Don't block startup over that.
  AppLogger.log('[SingleInstance] Could not establish a lock under any name; allowing start.');
  return true;
}

/// Returns true (we hold the lock), false (someone else does), or null if
/// this particular name was unusable and the caller should try the next one.
bool? _tryAcquire(String name) {
  final ffi.Pointer<Utf16> namePtr = name.toNativeUtf16();
  try {
    // Existence probe. A non-null handle means the object is already there,
    // which can only be true while another process holds it open.
    final int existing = _openMutexW(SYNCHRONIZE, 0, namePtr);
    if (existing != 0) {
      CloseHandle(existing);
      AppLogger.log('[SingleInstance] "$name" already exists -> another instance is running.');
      return false;
    }

    // Nothing there: claim it. Ownership is deliberately NOT requested
    // (bInitialOwner = FALSE) — mutex ownership belongs to a thread, and the
    // Dart VM may move an isolate between OS threads, which would abandon
    // the mutex while the app is still running. Existence alone is the
    // signal, and existence is tied to the process's open handle.
    final int handle = _createMutexW(ffi.nullptr, 0, namePtr);
    if (handle == 0) {
      AppLogger.log('[SingleInstance] CreateMutexW("$name") failed; trying next name.');
      return null;
    }

    _mutexHandle = handle;
    AppLogger.log('[SingleInstance] Acquired lock "$name" (handle=$handle).');
    return true;
  } finally {
    calloc.free(namePtr);
  }
}

/// Shown before the Flutter engine or any app service starts, so a blocked
/// second launch never starts a second Python engine.
void showAlreadyRunningMessage() {
  final ffi.Pointer<Utf16> text = 'Alpha Trader از قبل در حال اجراست.'.toNativeUtf16();
  final ffi.Pointer<Utf16> caption = 'Alpha Trader'.toNativeUtf16();
  try {
    MessageBox(0, text, caption, MB_OK | MB_ICONWARNING);
  } finally {
    calloc.free(text);
    calloc.free(caption);
  }
}

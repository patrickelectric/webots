#include <stdlib.h>
#include <webots/robot.h>
#include "../../../lib/ts_assertion.h"
#include "../../../lib/ts_utils.h"

#include <sys/types.h>
#include <unistd.h>
#define TIME_STEP 32

int main(int argc, char **argv) {
  if (argc == 1) {  // launched by Webots
    wb_robot_init();
    putenv("WEBOTS_SERVER=");  // clear environment variables set by Webots
    putenv("WEBOTS_ROBOT_ID=");
    putenv("WEBOTS_TMP_PATH=");
#ifdef _WIN32
    STARTUPINFO si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    // Start the child process.
    if (!CreateProcess(NULL, "extern.exe 2", NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi)) {
      printf("CreateProcess failed (%lu).\n", GetLastError());
      return EXIT_FAILURE;
    }
#else
    pid_t pid = fork();
    if (pid == 0) {
      if (system("./extern 2") != -1)
        return EXIT_SUCCESS;
      else
        return EXIT_FAILURE;
    }
#endif
    wb_robot_step(TIME_STEP);
#ifdef _WIN32
    WaitForSingleObject(pi.hProcess, INFINITE);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
#endif
    wb_robot_cleanup();
    return EXIT_SUCCESS;
  }
  ts_setup(argv[0]);
  ts_assert_int_equal(argc, 2, "Wrong number of arguments on the command line for starting extern controller.");
  ts_assert_string_equal(argv[1], "2", "Wrong argument for the extern controller: got \"%s\" instead of \"2\"");
  wb_robot_step(TIME_STEP);
  ts_send_success();
  return EXIT_SUCCESS;
}

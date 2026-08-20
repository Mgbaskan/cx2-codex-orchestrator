using System;
using System.Diagnostics;
using System.IO;
using System.Text;

internal static class CxLauncher
{
    /*
     * CX2_NATIVE_WINDOWS_LAUNCHER_V1
     *
     * Primary purpose:
     *   bypass cmd.exe / %* so an argv item containing an embedded
     *   newline reaches cx2_cli.py intact.
     *
     * The launcher receives its own argv from Windows and then
     * serializes those strings for Python using standard Windows
     * CRT-compatible quoting.
     */

    private static string QuoteArgument(string value)
    {
        if (value == null)
        {
            value = "";
        }

        if (value.Length == 0)
        {
            return "\"\"";
        }

        bool quote = false;

        for (int i = 0; i < value.Length; i++)
        {
            char c = value[i];

            if (Char.IsWhiteSpace(c) || c == '"')
            {
                quote = true;
                break;
            }
        }

        if (!quote)
        {
            return value;
        }

        StringBuilder result = new StringBuilder();

        result.Append('"');

        int backslashes = 0;

        for (int i = 0; i < value.Length; i++)
        {
            char c = value[i];

            if (c == '\\')
            {
                backslashes++;
                continue;
            }

            if (c == '"')
            {
                result.Append(
                    '\\',
                    (backslashes * 2) + 1
                );

                result.Append('"');

                backslashes = 0;
                continue;
            }

            if (backslashes > 0)
            {
                result.Append(
                    '\\',
                    backslashes
                );

                backslashes = 0;
            }

            result.Append(c);
        }

        if (backslashes > 0)
        {
            result.Append(
                '\\',
                backslashes * 2
            );
        }

        result.Append('"');

        return result.ToString();
    }


    private static string BuildArguments(
        string entrypoint,
        string[] args
    )
    {
        StringBuilder result = new StringBuilder();

        result.Append(
            QuoteArgument(
                entrypoint
            )
        );

        for (int i = 0; i < args.Length; i++)
        {
            result.Append(' ');

            result.Append(
                QuoteArgument(
                    args[i]
                )
            );
        }

        return result.ToString();
    }


    public static int Main(string[] args)
    {
        try
        {
            string bin = (
                AppDomain.CurrentDomain.BaseDirectory
            );

            string cxHome = Path.GetFullPath(
                Path.Combine(
                    bin,
                    ".."
                )
            );

            string python = Path.Combine(
                cxHome,
                "runtime",
                "venv",
                "Scripts",
                "python.exe"
            );

            string entrypoint = Path.Combine(
                cxHome,
                "runtime",
                "cx2",
                "cx2_cli.py"
            );

            if (!File.Exists(python))
            {
                Console.Error.WriteLine(
                    "[cx launcher] Python bulunamadi: "
                    + python
                );

                return 127;
            }

            if (!File.Exists(entrypoint))
            {
                Console.Error.WriteLine(
                    "[cx launcher] CX2 CLI bulunamadi: "
                    + entrypoint
                );

                return 127;
            }

            ProcessStartInfo start = (
                new ProcessStartInfo()
            );

            start.FileName = python;

            start.Arguments = BuildArguments(
                entrypoint,
                args
            );

            start.WorkingDirectory = (
                Environment.CurrentDirectory
            );

            start.UseShellExecute = false;
            start.CreateNoWindow = false;

            Process child = Process.Start(
                start
            );

            if (child == null)
            {
                Console.Error.WriteLine(
                    "[cx launcher] Python process baslatilamadi."
                );

                return 126;
            }

            child.WaitForExit();

            int exitCode = child.ExitCode;

            child.Dispose();

            return exitCode;
        }
        catch (Exception exc)
        {
            Console.Error.WriteLine(
                "[cx launcher] "
                + exc.GetType().Name
                + ": "
                + exc.Message
            );

            return 125;
        }
    }
}

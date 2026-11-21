using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Windows.Forms;

[assembly: AssemblyTitle("Tideline")]
[assembly: AssemblyProduct("Tideline")]
[assembly: AssemblyVersion(TidelineLauncher.Version)]
[assembly: AssemblyFileVersion(TidelineLauncher.Version)]
[assembly: AssemblyInformationalVersion(TidelineLauncher.Version)]

internal static class TidelineLauncher
{
    internal const string Version = "1.0.0.0";

    [STAThread]
    private static int Main(string[] args)
    {
        bool verify = args.Length == 1 && args[0] == "--verify";
        if (args.Length != 0 && !verify)
        {
            Console.Error.WriteLine("Usage: Tideline.exe [--verify]");
            return 2;
        }

        try
        {
            byte[] html = ReadBundle();
            string digest;
            using (SHA256 sha = SHA256.Create())
                digest = BitConverter.ToString(sha.ComputeHash(html)).Replace("-", "").ToLowerInvariant();
            if (digest != BuildInfo.BundleSha)
                throw new InvalidDataException("The embedded game failed its SHA-256 check.");

            if (verify)
            {
                Console.WriteLine("{{\"source_sha\":\"{0}\",\"bundle_sha256\":\"{1}\",\"version\":\"{2}\"}}",
                    BuildInfo.SourceSha, digest, Version);
            }
            else
            {
                LaunchGame(html);
            }

            return 0;
        }
        catch (Exception error)
        {
            if (verify)
                Console.Error.WriteLine(error.Message);
            else
                MessageBox.Show(error.Message, "Tideline", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static byte[] ReadBundle()
    {
        using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("Tideline.html"))
        using (var buffer = new MemoryStream())
        {
            if (resource == null)
                throw new InvalidDataException("The embedded game is missing.");
            resource.CopyTo(buffer);
            return buffer.ToArray();
        }
    }

    private static void LaunchGame(byte[] html)
    {
        // The browser opens the file asynchronously, so leave it available in this temporary directory.
        string directory = Path.Combine(Path.GetTempPath(), "Tideline-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        string path = Path.Combine(directory, "Tideline.html");
        using (var file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            file.Write(html, 0, html.Length);

        using (Process browser = Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }))
        {
            // ShellExecute may hand off to an existing browser process.
        }
    }
}

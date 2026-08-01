using System.Globalization;
using System.Reflection;
using System.Runtime.Loader;
using System.Diagnostics;
using System.Text.Json;

var exitCode = Run(args);
return exitCode;

static int Run(string[] args)
{
    if (args.Length == 0 || args.Contains("--help", StringComparer.OrdinalIgnoreCase))
    {
        PrintHelp();
        return 0;
    }

    try
    {
        var options = Options.Parse(args);
        Execute(options);
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine(DescribeException(exception));
        return 1;
    }
}

static void Execute(Options options)
{
    if (options.SkipClimate && !options.SkipBlocks && !options.SkipBlockMeta)
    {
        throw new InvalidOperationException(
            "--skip-climate requires --skip-block-meta or --skip-blocks because block metadata uses EPW climate data."
        );
    }

    var outputFolder = Path.GetFullPath(options.OutputFolder);
    Directory.CreateDirectory(outputFolder);

    var packageDir = ResolvePackageDirectory(options.PackageDir);
    ConfigureAssemblyResolution(packageDir);
    ConfigureNativeSearchPaths(packageDir);

    var coreAssembly = LoadAssembly(packageDir, "Urbano.Core.dll");
    var projectAssembly = LoadAssembly(packageDir, "ProjectSetup.dll");
    var protobufAssembly = LoadAssembly(packageDir, "protobuf-net.dll");
    var osmSharpAssembly = LoadAssembly(packageDir, "OsmSharp.dll");

    var keyType = RequireType(coreAssembly, "Urbano.Core.Data.Key");
    var coreKeys = ReadCoreKeys(keyType);

    var fileNameStr = string.IsNullOrWhiteSpace(options.FileNameStem)
        ? BuildFileNameString(options.BBox)
        : options.FileNameStem;
    var basePath = Path.Combine(outputFolder, fileNameStr);

    Console.WriteLine($"Package dir: {packageDir}");
    Console.WriteLine($"Output folder: {outputFolder}");
    Console.WriteLine($"File name stem: {fileNameStr}");

    var worldOriginType = RequireType(coreAssembly, "Urbano.Core.Process.WorldOrigin");
    var worldOrigin = Activator.CreateInstance(
        worldOriginType,
        options.BBox.Left,
        options.BBox.Bottom,
        options.BBox.Right
    ) ?? throw new InvalidOperationException("Failed to construct Urbano.Core.Process.WorldOrigin.");
    var utm = GetPropertyValue<string>(worldOrigin, "Utm");

    var downloadOsmType = RequireType(coreAssembly, "Urbano.Core.Engine.DownloadOsm");
    var progressBarType = RequireType(coreAssembly, "Urbano.Core.ProgressBar");
    var utilityType = RequireType(coreAssembly, "Urbano.Core.Process.Utility");
    var downloadClimateType = RequireType(coreAssembly, "Urbano.Core.Engine.DownloadClimate");
    var downloadBlocksType = RequireType(coreAssembly, "Urbano.Core.Engine.DownloadBlocks");
    var processBlocksType = RequireType(coreAssembly, "Urbano.Core.Engine.ProcessBlocks");
    var elevationExtensionsType = RequireType(coreAssembly, "Urbano.Core.Process.ElevationExtensions");
    var autoTierOptionsType = RequireType(coreAssembly, "Urbano.Core.Process.ElevationExtensions+AutoTierOptions");
    var downloadTiffType = RequireType(coreAssembly, "Urbano.Core.Process.TiffExtensions+DownloadTiffFile");
    var overtureType = RequireType(projectAssembly, "ProjectSetup.Overture");
    var serializerType = RequireType(protobufAssembly, "ProtoBuf.Serializer");
    var osmStreamTargetType = RequireType(osmSharpAssembly, "OsmSharp.Streams.OsmStreamTarget");
    var xmlOsmStreamSourceType = RequireType(osmSharpAssembly, "OsmSharp.Streams.XmlOsmStreamSource");
    var pbfOsmStreamTargetType = RequireType(osmSharpAssembly, "OsmSharp.Streams.PBFOsmStreamTarget");
    var locationType = RequireType(coreAssembly, "Urbano.Core.Data.Location");
    var elevationGridType = RequireType(coreAssembly, "Urbano.Core.Data.ElevationGrid");

    var listOfLocationType = typeof(List<>).MakeGenericType(locationType);

    string osmPath;
    if (!string.IsNullOrWhiteSpace(options.OsmFilePath))
    {
        osmPath = Path.GetFullPath(options.OsmFilePath);
        if (!FileHasContent(osmPath))
        {
            throw new FileNotFoundException("Supplied OSM file was not found or was empty.", osmPath);
        }

        Console.WriteLine($"Using supplied OSM: {osmPath}");
    }
    else
    {
        osmPath = EnsureOsm(
            downloadOsmType,
            progressBarType,
            basePath,
            options.BBox,
            coreKeys
        );
    }

    osmPath = EnsurePreferredOsmFormat(
        osmPath,
        basePath,
        coreKeys,
        osmStreamTargetType,
        xmlOsmStreamSourceType,
        pbfOsmStreamTargetType
    );

    string climatePath = string.Empty;
    if (!options.SkipBlocks && !options.SkipBlockMeta && !options.SkipClimate)
    {
        climatePath = EnsureClimate(
            downloadClimateType,
            progressBarType,
            utilityType,
            outputFolder,
            options.BBox
        );
    }

    string blockPath = string.Empty;
    if (!options.SkipBlocks)
    {
        var blocks = EnsureBlocks(
            downloadBlocksType,
            processBlocksType,
            serializerType,
            listOfLocationType,
            progressBarType,
            basePath,
            outputFolder,
            options.BBox,
            options.Granularity,
            utm,
            osmPath,
            climatePath,
            options.SkipBlockMeta,
            ResolveTravelerModelPath(options.TravelerModelPath, packageDir, coreKeys.TravelerModelFileName),
            coreKeys
        );

        SerializeToFile(serializerType, listOfLocationType, blocks, basePath + coreKeys.BlockExtension);
        blockPath = basePath + coreKeys.BlockExtension;
    }

    string elevationPath = string.Empty;
    if (!options.SkipElevation)
    {
        elevationPath = EnsureElevation(
            downloadTiffType,
            elevationExtensionsType,
            autoTierOptionsType,
            serializerType,
            elevationGridType,
            basePath,
            outputFolder,
            options.BBox,
            utm,
            coreKeys,
            options.ElevationTiffPath
        );
    }

    string overturePath = string.Empty;
    if (!options.SkipOverture)
    {
        overturePath = EnsureOverture(
            overtureType,
            progressBarType,
            basePath,
            options.BBox,
            coreKeys
        );
    }

    var coordinateReference = BuildCoordinateReferenceJson(worldOrigin);
    var projectSetting = new ProjectSettingJson
    {
        Folder = outputFolder,
        FileNameStr = fileNameStr,
        Granularity = options.Granularity,
        CoordinateReference = coordinateReference,
        Top = options.BBox.Top,
        Bottom = options.BBox.Bottom,
        Left = options.BBox.Left,
        Right = options.BBox.Right,
        OsmFilePath = osmPath,
        BlockFilePath = blockPath,
        ElevationFilePath = elevationPath,
        OvertureFilePath = overturePath,
        Layers = [],
    };

    var jsonOptions = new JsonSerializerOptions
    {
        WriteIndented = true,
    };
    var projectSettingPath = basePath + "_project_setting.json";
    File.WriteAllText(projectSettingPath, JsonSerializer.Serialize(projectSetting, jsonOptions));

    Console.WriteLine($"Project setting: {projectSettingPath}");
    Console.WriteLine($"OSM: {ValueOrEmpty(osmPath)}");
    Console.WriteLine($"Blocks: {ValueOrEmpty(blockPath)}");
    Console.WriteLine($"Elevation: {ValueOrEmpty(elevationPath)}");
    Console.WriteLine($"Overture: {ValueOrEmpty(overturePath)}");
}

static string EnsureOsm(
    Type downloadOsmType,
    Type progressBarType,
    string basePath,
    BBox bbox,
    CoreKeys coreKeys
)
{
    var xmlPath = basePath + coreKeys.OsmExtension;
    if (FileHasContent(xmlPath))
    {
        Console.WriteLine($"Reusing OSM: {xmlPath}");
        return xmlPath;
    }

    var pbfPath = basePath + coreKeys.OsmPbfExtension;
    if (FileHasContent(pbfPath))
    {
        Console.WriteLine($"Reusing OSM: {pbfPath}");
        return pbfPath;
    }

    Console.WriteLine("Downloading OSM...");
    var tryDownloadSmall = RequireMethod(downloadOsmType, "TryDownloadSmall", 6);
    var tryArgs = new object?[] { basePath, bbox.Top, bbox.Bottom, bbox.Right, bbox.Left, string.Empty };
    var smallDownloadWorked = (bool)(tryDownloadSmall.Invoke(null, tryArgs) ?? false);
    var finalPath = tryArgs[5] as string ?? string.Empty;
    if (smallDownloadWorked && !string.IsNullOrWhiteSpace(finalPath))
    {
        Console.WriteLine($"Downloaded OSM: {finalPath}");
        return finalPath;
    }

    if (!string.IsNullOrWhiteSpace(finalPath) && File.Exists(finalPath))
    {
        File.Delete(finalPath);
    }

    var downloadRun = RequireMethod(downloadOsmType, "Run", 9);
    var progressBar = Activator.CreateInstance(progressBarType, '#', '-')!;
    finalPath = downloadRun.Invoke(
        null,
        [
            CancellationToken.None,
            progressBar,
            0.2d,
            basePath,
            bbox.Top,
            bbox.Bottom,
            bbox.Right,
            bbox.Left,
            true,
        ]
    ) as string ?? string.Empty;

    if (string.IsNullOrWhiteSpace(finalPath) || !File.Exists(finalPath))
    {
        throw new InvalidOperationException("Urbano did not produce an OSM file.");
    }

    Console.WriteLine($"Downloaded OSM: {finalPath}");
    return finalPath;
}

static string EnsurePreferredOsmFormat(
    string osmPath,
    string basePath,
    CoreKeys coreKeys,
    Type osmStreamTargetType,
    Type xmlOsmStreamSourceType,
    Type pbfOsmStreamTargetType
)
{
    if (string.IsNullOrWhiteSpace(osmPath) || !File.Exists(osmPath))
    {
        throw new FileNotFoundException("OSM file was not found.", osmPath);
    }

    if (osmPath.EndsWith(coreKeys.OsmPbfExtension, StringComparison.OrdinalIgnoreCase))
    {
        return osmPath;
    }

    if (!osmPath.EndsWith(coreKeys.OsmExtension, StringComparison.OrdinalIgnoreCase))
    {
        return osmPath;
    }

    var pbfPath = basePath + coreKeys.OsmPbfExtension;
    if (FileHasContent(pbfPath))
    {
        Console.WriteLine($"Reusing OSM PBF: {pbfPath}");
        return pbfPath;
    }

    DeleteIfExists(pbfPath);
    Console.WriteLine($"Converting OSM XML to PBF: {pbfPath}");
    ConvertOsmXmlToPbf(osmPath, pbfPath, osmStreamTargetType, xmlOsmStreamSourceType, pbfOsmStreamTargetType);

    if (!FileHasContent(pbfPath))
    {
        throw new InvalidOperationException("OSM XML to PBF conversion did not produce a valid .osm.pbf file.");
    }

    return pbfPath;
}

static void ConvertOsmXmlToPbf(
    string xmlPath,
    string pbfPath,
    Type osmStreamTargetType,
    Type xmlOsmStreamSourceType,
    Type pbfOsmStreamTargetType
)
{
    var tempPbfPath = pbfPath + ".tmp";
    DeleteIfExists(tempPbfPath);

    var registerSource = RequireMethod(osmStreamTargetType, "RegisterSource", 1, method => !method.IsStatic);
    var pull = RequireMethod(osmStreamTargetType, "Pull", 0, method => !method.IsStatic);
    var close = RequireMethod(osmStreamTargetType, "Close", 0, method => !method.IsStatic);

    Exception? conversionError = null;
    {
        using var xmlStream = File.OpenRead(xmlPath);
        using var pbfStream = File.Create(tempPbfPath);

        object? xmlSource = null;
        object? pbfTarget = null;
        try
        {
            xmlSource = Activator.CreateInstance(xmlOsmStreamSourceType, xmlStream)
                ?? throw new InvalidOperationException("Failed to construct OsmSharp XmlOsmStreamSource.");
            pbfTarget = Activator.CreateInstance(pbfOsmStreamTargetType, pbfStream, false, null, null)
                ?? throw new InvalidOperationException("Failed to construct OsmSharp PBFOsmStreamTarget.");

            registerSource.Invoke(pbfTarget, [xmlSource]);
            pull.Invoke(pbfTarget, []);
            close.Invoke(pbfTarget, []);
            pbfStream.Flush();
        }
        catch (Exception exception)
        {
            conversionError = exception;
        }
        finally
        {
            DisposeIfNeeded(pbfTarget);
            DisposeIfNeeded(xmlSource);
        }
    }

    if (conversionError is not null)
    {
        DeleteIfExists(tempPbfPath);
        throw new InvalidOperationException("Failed to convert OSM XML to PBF.", conversionError);
    }

    File.Move(tempPbfPath, pbfPath, true);
}

static string EnsureClimate(
    Type downloadClimateType,
    Type progressBarType,
    Type utilityType,
    string outputFolder,
    BBox bbox
)
{
    Console.WriteLine("Downloading climate EPW...");
    var getEmbeddedResourceStream = RequireMethod(
        utilityType,
        "GetEmbeddedResourceStream",
        1,
        method => method.IsStatic
    );
    using var climateDictionaryStream = (Stream?)getEmbeddedResourceStream.Invoke(
        null,
        ["Urbano.Core.Resources.USA_TMYx_EPW.csv"]
    ) ?? throw new InvalidOperationException("Failed to open the embedded Urbano climate dictionary.");

    var run = RequireMethod(downloadClimateType, "Run", 11);
    var progressBar = Activator.CreateInstance(progressBarType, '#', '-')!;
    var args = new object?[]
    {
        CancellationToken.None,
        progressBar,
        0.1d,
        outputFolder,
        climateDictionaryStream,
        bbox.Top,
        bbox.Bottom,
        bbox.Right,
        bbox.Left,
        string.Empty,
        true,
    };
    run.Invoke(null, args);

    var epwPath = args[9] as string ?? string.Empty;
    if (string.IsNullOrWhiteSpace(epwPath) || !File.Exists(epwPath))
    {
        throw new InvalidOperationException(
            "Urbano did not produce an EPW climate file. This step is only reliable for U.S. bounds."
        );
    }

    Console.WriteLine($"Climate EPW: {epwPath}");
    return epwPath;
}

static object EnsureBlocks(
    Type downloadBlocksType,
    Type processBlocksType,
    Type serializerType,
    Type listOfLocationType,
    Type progressBarType,
    string basePath,
    string outputFolder,
    BBox bbox,
    string granularity,
    string utm,
    string osmPath,
    string climatePath,
    bool skipBlockMeta,
    string travelerModelPath,
    CoreKeys coreKeys
)
{
    var blockPath = basePath + coreKeys.BlockExtension;
    object blocks;

    if (FileHasContent(blockPath))
    {
        Console.WriteLine($"Reusing blocks: {blockPath}");
        blocks = DeserializeFromFile(serializerType, listOfLocationType, blockPath);
    }
    else
    {
        DeleteIfExists(blockPath);
        Console.WriteLine("Downloading census blocks...");
        var run = RequireMethod(downloadBlocksType, "Run", 6);
        blocks = run.Invoke(
            null,
            [bbox.Top, bbox.Bottom, bbox.Right, bbox.Left, outputFolder, granularity]
        ) ?? throw new InvalidOperationException("Urbano did not return any block data.");
    }

    if (!skipBlockMeta)
    {
        if (string.IsNullOrWhiteSpace(osmPath) || !File.Exists(osmPath))
        {
            throw new InvalidOperationException("Block metadata needs a valid OSM file path.");
        }

        if (string.IsNullOrWhiteSpace(travelerModelPath) || !File.Exists(travelerModelPath))
        {
            throw new FileNotFoundException(
                "The Urbano traveler ONNX model was not found. Supply --traveler-model or install a package version that contains it.",
                travelerModelPath
            );
        }

        Console.WriteLine("Completing block metadata...");
        var completeBlockMeta = RequireMethod(processBlocksType, "CompleteBlockMeta", 7);
        completeBlockMeta.Invoke(
            null,
            [blocks, outputFolder, osmPath, travelerModelPath, climatePath, granularity, utm]
        );
    }

    return blocks;
}

static string EnsureElevation(
    Type downloadTiffType,
    Type elevationExtensionsType,
    Type autoTierOptionsType,
    Type serializerType,
    Type elevationGridType,
    string basePath,
    string outputFolder,
    BBox bbox,
    string utm,
    CoreKeys coreKeys,
    string? suppliedTiffPath
)
{
    var elevationPath = basePath + coreKeys.ElevationExtension;
    if (FileHasContent(elevationPath))
    {
        Console.WriteLine($"Reusing elevation grid: {elevationPath}");
        return elevationPath;
    }

    DeleteIfExists(elevationPath);
    var bounds = (bbox.Left, bbox.Right, bbox.Bottom, bbox.Top);
    string tiffPath = string.Empty;

    if (!string.IsNullOrWhiteSpace(suppliedTiffPath))
    {
        tiffPath = Path.GetFullPath(suppliedTiffPath);
        if (!FileHasContent(tiffPath))
        {
            throw new FileNotFoundException("Supplied elevation TIFF was not found or was empty.", tiffPath);
        }

        Console.WriteLine($"Using supplied elevation TIFF: {tiffPath}");
    }
    else
    {
        Console.WriteLine("Downloading elevation TIFF and building .egrid...");
        var downloadTiff = RequireMethod(downloadTiffType, "DownloadUsgs3DepTiffForBounds", 7);
        Exception? lastDownloadError = null;
        for (var attempt = 1; attempt <= 3; attempt++)
        {
            try
            {
                tiffPath = downloadTiff.Invoke(
                    null,
                    [bounds, 2.0d, outputFolder, 200.0d, null, 8192, false]
                ) as string ?? string.Empty;
                lastDownloadError = null;
                break;
            }
            catch (Exception exception) when (IsTransientElevationError(exception) && attempt < 3)
            {
                lastDownloadError = exception;
                var sleepSeconds = 5 * attempt;
                Console.WriteLine(
                    $"USGS elevation retry {attempt}/3 after transient failure: {exception.GetBaseException().Message}"
                );
                Thread.Sleep(TimeSpan.FromSeconds(sleepSeconds));
            }
        }

        if (lastDownloadError is not null)
        {
            throw lastDownloadError;
        }

        if (string.IsNullOrWhiteSpace(tiffPath) || !File.Exists(tiffPath))
        {
            throw new InvalidOperationException(
                "Urbano did not produce a USGS TIFF. This step is only reliable for U.S. bounds."
            );
        }
    }

    var buildElevationGridFromTiff = RequireMethod(elevationExtensionsType, "BuildElevationGridFromTiff", 5);
    var autoTierOptions = Activator.CreateInstance(autoTierOptionsType);
    var elevationGrid = buildElevationGridFromTiff.Invoke(
        null,
        [bounds, 200.0d, utm, tiffPath, autoTierOptions]
    ) ?? throw new InvalidOperationException("Urbano did not build an elevation grid.");

    SerializeToFile(serializerType, elevationGridType, elevationGrid, elevationPath);
    Console.WriteLine($"Elevation grid: {elevationPath}");
    return elevationPath;
}

static string EnsureOverture(
    Type overtureType,
    Type progressBarType,
    string basePath,
    BBox bbox,
    CoreKeys coreKeys
)
{
    var overturePath = basePath + "_overture" + coreKeys.ParquetExtension;
    if (FileHasContent(overturePath))
    {
        Console.WriteLine($"Reusing Overture parquet: {overturePath}");
        return overturePath;
    }

    DeleteIfExists(overturePath);
    Console.WriteLine("Downloading Overture buildings parquet...");
    try
    {
        var download = RequireMethod(overtureType, "Download", 7);
        var progressBar = Activator.CreateInstance(progressBarType, '#', '-')!;
        download.Invoke(
            null,
            [progressBar, 0.15d, bbox.Top, bbox.Bottom, bbox.Left, bbox.Right, overturePath]
        );
    }
    catch (Exception exception)
    {
        Console.WriteLine($"Native Overture download failed, falling back to overturemaps CLI: {exception.GetBaseException().Message}");
        RunOvertureCli(overturePath, bbox);
    }

    if (!File.Exists(overturePath))
    {
        throw new InvalidOperationException("Urbano did not produce an Overture parquet file.");
    }

    Console.WriteLine($"Overture parquet: {overturePath}");
    return overturePath;
}

static void RunOvertureCli(string outputPath, BBox bbox)
{
    var process = new Process
    {
        StartInfo = new ProcessStartInfo
        {
            FileName = "overturemaps",
            Arguments = string.Join(
                " ",
                [
                    "download",
                    $"--bbox={FormatCoordinate(bbox.Left)},{FormatCoordinate(bbox.Bottom)},{FormatCoordinate(bbox.Right)},{FormatCoordinate(bbox.Top)}",
                    "-f",
                    "geoparquet",
                    "--type",
                    "building",
                    "--output",
                    $"\"{outputPath}\"",
                ]
            ),
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        },
    };

    process.Start();
    var stdout = process.StandardOutput.ReadToEnd();
    var stderr = process.StandardError.ReadToEnd();
    process.WaitForExit();

    if (process.ExitCode != 0)
    {
        throw new InvalidOperationException(
            "overturemaps CLI failed to produce the fallback geoparquet file." +
            Environment.NewLine +
            (string.IsNullOrWhiteSpace(stderr) ? stdout : stderr)
        );
    }
}

static object DeserializeFromFile(Type serializerType, Type targetType, string path)
{
    using var stream = File.OpenRead(path);
    var deserialize = serializerType
        .GetMethods(BindingFlags.Public | BindingFlags.Static)
        .Single(method =>
            method.Name == "Deserialize" &&
            method.IsGenericMethodDefinition &&
            method.GetParameters().Length == 1 &&
            typeof(Stream).IsAssignableFrom(method.GetParameters()[0].ParameterType)
        )
        .MakeGenericMethod(targetType);
    return deserialize.Invoke(null, [stream])
        ?? throw new InvalidOperationException($"Failed to deserialize {path}.");
}

static void SerializeToFile(Type serializerType, Type targetType, object payload, string path)
{
    using var stream = File.Create(path);
    var serialize = serializerType
        .GetMethods(BindingFlags.Public | BindingFlags.Static)
        .Single(method =>
            method.Name == "Serialize" &&
            method.IsGenericMethodDefinition &&
            method.GetParameters().Length == 2 &&
            typeof(Stream).IsAssignableFrom(method.GetParameters()[0].ParameterType)
        )
        .MakeGenericMethod(targetType);
    serialize.Invoke(null, [stream, payload]);
}

static string ResolveTravelerModelPath(string? explicitPath, string packageDir, string fileName)
{
    if (!string.IsNullOrWhiteSpace(explicitPath))
    {
        var fullPath = Path.GetFullPath(explicitPath);
        if (!File.Exists(fullPath))
        {
            throw new FileNotFoundException("Traveler ONNX model was not found.", fullPath);
        }

        return fullPath;
    }

    var directCandidate = Path.Combine(packageDir, "Resources", fileName);
    if (File.Exists(directCandidate))
    {
        return directCandidate;
    }

    var packageRoot = Directory.GetParent(packageDir)?.Parent?.FullName;
    if (packageRoot is not null && Directory.Exists(packageRoot))
    {
        var candidate = Directory
            .EnumerateFiles(packageRoot, fileName, SearchOption.AllDirectories)
            .OrderByDescending(path => ParseVersionFromPath(path))
            .FirstOrDefault();
        if (candidate is not null)
        {
            return candidate;
        }
    }

    return string.Empty;
}

static Version ParseVersionFromPath(string path)
{
    foreach (var segment in path.Split(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar))
    {
        if (Version.TryParse(segment, out var version))
        {
            return version;
        }
    }

    return new Version(0, 0);
}

static CoordinateReferenceJson BuildCoordinateReferenceJson(object worldOrigin)
{
    var bottomLeft = GetProperty(worldOrigin, "BottomLeftUtm");
    var bottomRight = GetProperty(worldOrigin, "BottomRightUtm");

    return new CoordinateReferenceJson
    {
        Utm = GetPropertyValue<string>(worldOrigin, "Utm"),
        BottomLeftUtm = new UtmPointJson
        {
            Item1 = Convert.ToDouble(GetMemberValue(bottomLeft, "Item1"), CultureInfo.InvariantCulture),
            Item2 = Convert.ToDouble(GetMemberValue(bottomLeft, "Item2"), CultureInfo.InvariantCulture),
        },
        BottomRightUtm = new UtmPointJson
        {
            Item1 = Convert.ToDouble(GetMemberValue(bottomRight, "Item1"), CultureInfo.InvariantCulture),
            Item2 = Convert.ToDouble(GetMemberValue(bottomRight, "Item2"), CultureInfo.InvariantCulture),
        },
    };
}

static object GetProperty(object instance, string propertyName)
{
    return instance.GetType().GetProperty(propertyName, BindingFlags.Public | BindingFlags.Instance)?.GetValue(instance)
        ?? throw new InvalidOperationException(
            $"Property {propertyName} was not found on {instance.GetType().FullName}."
        );
}

static T GetPropertyValue<T>(object instance, string propertyName)
{
    var value = GetProperty(instance, propertyName);
    return (T)Convert.ChangeType(value, typeof(T), CultureInfo.InvariantCulture);
}

static object GetMemberValue(object instance, string memberName)
{
    var property = instance.GetType().GetProperty(memberName, BindingFlags.Public | BindingFlags.Instance);
    if (property is not null)
    {
        return property.GetValue(instance)
            ?? throw new InvalidOperationException(
                $"Property {memberName} on {instance.GetType().FullName} was null."
            );
    }

    var field = instance.GetType().GetField(memberName, BindingFlags.Public | BindingFlags.Instance);
    if (field is not null)
    {
        return field.GetValue(instance)
            ?? throw new InvalidOperationException(
                $"Field {memberName} on {instance.GetType().FullName} was null."
            );
    }

    throw new InvalidOperationException(
        $"Member {memberName} was not found on {instance.GetType().FullName}."
    );
}

static CoreKeys ReadCoreKeys(Type keyType)
{
    return new CoreKeys(
        GetStaticStringField(keyType, "OSM_EXTENSION"),
        GetStaticStringField(keyType, "OSM_PBF_EXTENSION"),
        GetStaticStringField(keyType, "BLOCK_EXTENSION"),
        GetStaticStringField(keyType, "ELEVATION_EXTENSION"),
        GetStaticStringField(keyType, "PARQUET_EXTENSION"),
        GetStaticStringField(keyType, "URBANO_ONNX_MODEL_FILENAME_TRAVELER")
    );
}

static string GetStaticStringField(Type type, string name)
{
    var field = type.GetField(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)
        ?? throw new InvalidOperationException($"Static field {type.FullName}::{name} was not found.");
    return field.GetValue(null) as string
        ?? throw new InvalidOperationException($"Static field {type.FullName}::{name} was null.");
}

static Assembly LoadAssembly(string packageDir, string assemblyFileName)
{
    var assemblyPath = Path.Combine(packageDir, assemblyFileName);
    if (!File.Exists(assemblyPath))
    {
        throw new FileNotFoundException($"Missing required assembly: {assemblyPath}", assemblyPath);
    }

    return AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyPath);
}

static void ConfigureAssemblyResolution(string packageDir)
{
    AssemblyLoadContext.Default.Resolving += (_, name) =>
    {
        var candidate = Path.Combine(packageDir, $"{name.Name}.dll");
        if (File.Exists(candidate))
        {
            return AssemblyLoadContext.Default.LoadFromAssemblyPath(candidate);
        }

        return null;
    };
}

static void ConfigureNativeSearchPaths(string packageDir)
{
    var extraPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        packageDir,
        Path.Combine(packageDir, "runtimes", "win-x64", "native"),
    };

    var packageRoot = Directory.GetParent(packageDir)?.Parent?.FullName;
    if (packageRoot is not null && Directory.Exists(packageRoot))
    {
        foreach (var nativeFile in Directory.EnumerateFiles(packageRoot, "duckdb.dll", SearchOption.AllDirectories))
        {
            var directory = Path.GetDirectoryName(nativeFile);
            if (!string.IsNullOrWhiteSpace(directory))
            {
                extraPaths.Add(directory);
            }
        }

        foreach (var nativeFile in Directory.EnumerateFiles(packageRoot, "onnxruntime.dll", SearchOption.AllDirectories))
        {
            var directory = Path.GetDirectoryName(nativeFile);
            if (!string.IsNullOrWhiteSpace(directory))
            {
                extraPaths.Add(directory);
            }
        }
    }

    var currentPath = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
    var mergedPath = string.Join(
        ";",
        extraPaths.Where(Directory.Exists).Concat([currentPath]).Where(path => !string.IsNullOrWhiteSpace(path))
    );
    Environment.SetEnvironmentVariable("PATH", mergedPath);
}

static string ResolvePackageDirectory(string? explicitPackageDir)
{
    if (!string.IsNullOrWhiteSpace(explicitPackageDir))
    {
        var fullPath = Path.GetFullPath(explicitPackageDir);
        if (!Directory.Exists(fullPath))
        {
            throw new DirectoryNotFoundException($"Urbano package directory was not found: {fullPath}");
        }

        return fullPath;
    }

    var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
    var packageRoot = Path.Combine(appData, "McNeel", "Rhinoceros", "packages", "8.0", "Urbano2");
    if (!Directory.Exists(packageRoot))
    {
        throw new DirectoryNotFoundException($"Urbano package root was not found: {packageRoot}");
    }

    var packageDir = Directory
        .EnumerateDirectories(packageRoot)
        .Select(path => new
        {
            Path = path,
            Version = Version.TryParse(Path.GetFileName(path), out var version) ? version : null,
        })
        .Where(item => item.Version is not null)
        .OrderByDescending(item => item.Version)
        .Select(item => item.Path)
        .FirstOrDefault(path =>
            File.Exists(Path.Combine(path, "Urbano.Core.dll")) &&
            File.Exists(Path.Combine(path, "ProjectSetup.dll"))
        );

    if (packageDir is null)
    {
        throw new DirectoryNotFoundException(
            $"No installed Urbano package with Urbano.Core.dll and ProjectSetup.dll was found under {packageRoot}."
        );
    }

    return packageDir;
}

static Type RequireType(Assembly assembly, string fullName)
{
    return assembly.GetType(fullName)
        ?? throw new InvalidOperationException($"Type {fullName} was not found in {assembly.Location}.");
}

static MethodInfo RequireMethod(Type type, string methodName, int parameterCount, Func<MethodInfo, bool>? predicate = null)
{
    var method = type
        .GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance)
        .SingleOrDefault(candidate =>
            candidate.Name == methodName &&
            candidate.GetParameters().Length == parameterCount &&
            (predicate?.Invoke(candidate) ?? true)
        );

    return method
        ?? throw new InvalidOperationException(
            $"Method {type.FullName}::{methodName} with {parameterCount} parameters was not found."
        );
}

static string BuildFileNameString(BBox bbox)
{
    return string.Join(
        "_",
        FormatCoordinate(bbox.Top),
        FormatCoordinate(bbox.Bottom),
        FormatCoordinate(bbox.Right),
        FormatCoordinate(bbox.Left)
    );
}

static string FormatCoordinate(double value)
{
    return value.ToString("0.#######", CultureInfo.InvariantCulture);
}

static string ValueOrEmpty(string value)
{
    return string.IsNullOrWhiteSpace(value) ? "<empty>" : value;
}

static bool IsTransientElevationError(Exception exception)
{
    var message = exception.GetBaseException().Message;
    return message.Contains("504", StringComparison.OrdinalIgnoreCase)
        || message.Contains("time-out", StringComparison.OrdinalIgnoreCase)
        || message.Contains("timed out", StringComparison.OrdinalIgnoreCase)
        || message.Contains("502", StringComparison.OrdinalIgnoreCase)
        || message.Contains("503", StringComparison.OrdinalIgnoreCase);
}

static bool FileHasContent(string path)
{
    return File.Exists(path) && new FileInfo(path).Length > 0;
}

static void DisposeIfNeeded(object? instance)
{
    if (instance is IDisposable disposable)
    {
        disposable.Dispose();
    }
}

static void DeleteIfExists(string path)
{
    if (File.Exists(path))
    {
        File.Delete(path);
    }
}

static void PrintHelp()
{
    Console.WriteLine(
        """
        Usage:
          UrbanoBridge --bbox west,south,east,north --output-folder PATH [options]

        Options:
          --bbox VALUE             Bounding box as west,south,east,north.
          --output-folder PATH     Folder that will receive the Urbano project files.
          --granularity VALUE      Project granularity. Default: Block
          --package-dir PATH       Explicit Urbano package directory.
          --osm-file-path PATH     Use an existing .osm or .osm.pbf file instead of downloading OSM natively.
          --elevation-tiff-path PATH
                                  Use an existing GeoTIFF when building .egrid instead of downloading USGS TIFF natively.
          --traveler-model PATH    Explicit path to the Urbano traveler ONNX model.
          --skip-climate           Skip EPW climate lookup.
          --skip-blocks            Skip .blocks generation.
          --skip-block-meta        Skip the block metadata enrichment pass.
          --skip-elevation         Skip .egrid generation.
          --skip-overture          Skip Overture parquet generation.

        Notes:
          - The native Urbano census, climate, and elevation steps are U.S.-specific.
          - Overture output is written as <stem>_overture.parquet because that is what ProjectSetup.dll produces.
        """
    );
}

static string DescribeException(Exception exception)
{
    var parts = new List<string>();
    Exception? current = exception;
    while (current is not null)
    {
        parts.Add(current.ToString());
        current = current.InnerException;
    }

    return string.Join(Environment.NewLine + Environment.NewLine + "Inner:" + Environment.NewLine, parts);
}

sealed class Options
{
    public required BBox BBox { get; init; }
    public required string OutputFolder { get; init; }
    public required string Granularity { get; init; }
    public string? PackageDir { get; init; }
    public string? OsmFilePath { get; init; }
    public string? ElevationTiffPath { get; init; }
    public string? TravelerModelPath { get; init; }
    public string? FileNameStem { get; init; }
    public bool SkipClimate { get; init; }
    public bool SkipBlocks { get; init; }
    public bool SkipBlockMeta { get; init; }
    public bool SkipElevation { get; init; }
    public bool SkipOverture { get; init; }

    public static Options Parse(string[] args)
    {
        string? bbox = null;
        string? outputFolder = null;
        string granularity = "Block";
        string? packageDir = null;
        string? osmFilePath = null;
        string? elevationTiffPath = null;
        string? travelerModelPath = null;
        string? fileNameStem = null;
        var skipClimate = false;
        var skipBlocks = false;
        var skipBlockMeta = false;
        var skipElevation = false;
        var skipOverture = false;

        for (var index = 0; index < args.Length; index++)
        {
            var argument = args[index];
            switch (argument)
            {
                case "--bbox":
                    bbox = NextValue(args, ref index, argument);
                    break;
                case "--output-folder":
                    outputFolder = NextValue(args, ref index, argument);
                    break;
                case "--granularity":
                    granularity = NextValue(args, ref index, argument);
                    break;
                case "--package-dir":
                    packageDir = NextValue(args, ref index, argument);
                    break;
                case "--osm-file-path":
                    osmFilePath = NextValue(args, ref index, argument);
                    break;
                case "--elevation-tiff-path":
                    elevationTiffPath = NextValue(args, ref index, argument);
                    break;
                case "--traveler-model":
                    travelerModelPath = NextValue(args, ref index, argument);
                    break;
                case "--file-name-stem":
                    fileNameStem = NextValue(args, ref index, argument);
                    break;
                case "--skip-climate":
                    skipClimate = true;
                    break;
                case "--skip-blocks":
                    skipBlocks = true;
                    break;
                case "--skip-block-meta":
                    skipBlockMeta = true;
                    break;
                case "--skip-elevation":
                    skipElevation = true;
                    break;
                case "--skip-overture":
                    skipOverture = true;
                    break;
                default:
                    throw new ArgumentException($"Unknown argument: {argument}");
            }
        }

        if (string.IsNullOrWhiteSpace(bbox))
        {
            throw new ArgumentException("--bbox is required.");
        }

        if (string.IsNullOrWhiteSpace(outputFolder))
        {
            throw new ArgumentException("--output-folder is required.");
        }

        return new Options
        {
            BBox = BBox.Parse(bbox),
            OutputFolder = outputFolder,
            Granularity = granularity,
            PackageDir = packageDir,
            OsmFilePath = osmFilePath,
            ElevationTiffPath = elevationTiffPath,
            TravelerModelPath = travelerModelPath,
            FileNameStem = fileNameStem,
            SkipClimate = skipClimate,
            SkipBlocks = skipBlocks,
            SkipBlockMeta = skipBlockMeta,
            SkipElevation = skipElevation,
            SkipOverture = skipOverture,
        };
    }

    static string NextValue(string[] args, ref int index, string argument)
    {
        if (index + 1 >= args.Length)
        {
            throw new ArgumentException($"Missing value for {argument}.");
        }

        index += 1;
        return args[index];
    }
}

readonly record struct BBox(double Left, double Bottom, double Right, double Top)
{
    public static BBox Parse(string value)
    {
        var parts = value
            .Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length != 4)
        {
            throw new ArgumentException(
                "BBox must have 4 comma-separated values: west,south,east,north."
            );
        }

        var numbers = parts
            .Select(part => double.Parse(part, CultureInfo.InvariantCulture))
            .ToArray();

        var left = Math.Min(numbers[0], numbers[2]);
        var right = Math.Max(numbers[0], numbers[2]);
        var bottom = Math.Min(numbers[1], numbers[3]);
        var top = Math.Max(numbers[1], numbers[3]);

        return new BBox(left, bottom, right, top);
    }
}

readonly record struct CoreKeys(
    string OsmExtension,
    string OsmPbfExtension,
    string BlockExtension,
    string ElevationExtension,
    string ParquetExtension,
    string TravelerModelFileName
);

sealed class ProjectSettingJson
{
    public required string Folder { get; init; }
    public required string FileNameStr { get; init; }
    public required string Granularity { get; init; }
    public required CoordinateReferenceJson CoordinateReference { get; init; }
    public required double Top { get; init; }
    public required double Bottom { get; init; }
    public required double Left { get; init; }
    public required double Right { get; init; }
    public required string OsmFilePath { get; init; }
    public required string BlockFilePath { get; init; }
    public required string ElevationFilePath { get; init; }
    public required string OvertureFilePath { get; init; }
    public required List<string> Layers { get; init; }
}

sealed class CoordinateReferenceJson
{
    public required string Utm { get; init; }
    public required UtmPointJson BottomLeftUtm { get; init; }
    public required UtmPointJson BottomRightUtm { get; init; }
}

sealed class UtmPointJson
{
    public required double Item1 { get; init; }
    public required double Item2 { get; init; }
}

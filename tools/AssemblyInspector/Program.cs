using Mono.Cecil;
using Mono.Cecil.Cil;

if (args.Length == 0)
{
    Console.Error.WriteLine("Usage: AssemblyInspector <assembly-path> [name-filter] [--calls] [--resources] [--il]");
    return 1;
}

var assemblyPath = args[0];
var filter = args.Length > 1 && !string.Equals(args[1], "--calls", StringComparison.OrdinalIgnoreCase)
                        && !string.Equals(args[1], "--resources", StringComparison.OrdinalIgnoreCase)
                        && !string.Equals(args[1], "--il", StringComparison.OrdinalIgnoreCase)
    ? args[1]
    : null;
var showCalls = args.Any(arg => string.Equals(arg, "--calls", StringComparison.OrdinalIgnoreCase));
var showResources = args.Any(arg => string.Equals(arg, "--resources", StringComparison.OrdinalIgnoreCase));
var showIl = args.Any(arg => string.Equals(arg, "--il", StringComparison.OrdinalIgnoreCase));

var assembly = AssemblyDefinition.ReadAssembly(assemblyPath);
if (showResources)
{
    foreach (var resource in assembly.MainModule.Resources.OrderBy(resource => resource.Name))
    {
        if (!Matches(resource.Name, filter))
        {
            continue;
        }

        Console.WriteLine($"{resource.ResourceType} {resource.Name}");
    }

    return 0;
}

foreach (var module in assembly.Modules)
{
    foreach (var type in EnumerateTypes(module.Types).OrderBy(t => t.FullName))
    {
        if (!Matches(type.FullName, filter) && !type.Methods.Any(m => Matches(m.FullName, filter)))
        {
            continue;
        }

        Console.WriteLine(type.FullName);
        foreach (var method in type.Methods.OrderBy(m => m.Name))
        {
            if (!Matches(type.FullName, filter) && !Matches(method.FullName, filter))
            {
                continue;
            }

            Console.WriteLine($"  {method.Attributes} {method.ReturnType.FullName} {method.Name}({string.Join(", ", method.Parameters.Select(p => p.ParameterType.FullName + " " + p.Name))})");
            if (showCalls)
            {
                foreach (var called in GetCalledMethods(method))
                {
                    Console.WriteLine($"    -> {called}");
                }
            }
            if (showIl)
            {
                foreach (var instruction in GetInstructions(method))
                {
                    Console.WriteLine($"    {instruction}");
                }
            }
        }
    }
}

return 0;

static bool Matches(string value, string? filter)
{
    return string.IsNullOrWhiteSpace(filter) ||
           value.Contains(filter, StringComparison.OrdinalIgnoreCase);
}

static IEnumerable<string> GetCalledMethods(MethodDefinition method)
{
    if (!method.HasBody)
    {
        yield break;
    }

    var seen = new HashSet<string>(StringComparer.Ordinal);
    foreach (var instruction in method.Body.Instructions)
    {
        if ((instruction.OpCode.Code != Code.Call && instruction.OpCode.Code != Code.Callvirt) ||
            instruction.Operand is not MethodReference called)
        {
            continue;
        }

        var signature = called.FullName;
        if (seen.Add(signature))
        {
            yield return signature;
        }
    }
}

static IEnumerable<string> GetInstructions(MethodDefinition method)
{
    if (!method.HasBody)
    {
        yield break;
    }

    foreach (var instruction in method.Body.Instructions)
    {
        var operand = instruction.Operand switch
        {
            MethodReference methodReference => methodReference.FullName,
            FieldReference fieldReference => fieldReference.FullName,
            TypeReference typeReference => typeReference.FullName,
            ParameterDefinition parameterDefinition => parameterDefinition.Name,
            VariableDefinition variableDefinition => $"V_{variableDefinition.Index}",
            Instruction targetInstruction => $"IL_{targetInstruction.Offset:x4}",
            Instruction[] targetInstructions => string.Join(", ", targetInstructions.Select(target => $"IL_{target.Offset:x4}")),
            null => string.Empty,
            _ => instruction.Operand.ToString() ?? string.Empty,
        };

        yield return $"IL_{instruction.Offset:x4}: {instruction.OpCode} {operand}".TrimEnd();
    }
}

static IEnumerable<TypeDefinition> EnumerateTypes(IEnumerable<TypeDefinition> types)
{
    foreach (var type in types)
    {
        yield return type;
        foreach (var nested in EnumerateTypes(type.NestedTypes))
        {
            yield return nested;
        }
    }
}

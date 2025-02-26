# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from io import StringIO
from json import loads
from logging import Logger
from typing import Any, Callable, Optional, Tuple

from agent.logic.engine_strategy import EngineStrategy, SolverOutcome
from agent.logic.logic_py_c_harness_generator import LogicPyCHarnessGenerator
from libcst import MetadataWrapper, Module


# Instructs the model to generate the solution constraints.
_CONSTRAINTS_MESSAGE: str = """Now you must generate a validation function which contains constraints that assert that a given solution is correct. Your solver tool will then find a solution which satisfies your constraints and thus solve the puzzle. Please adhere to the following rules:

* Express your constraints in Python, but do not use any loops or comprehensions.
* Do not generate constraints that are already enforced by the data type you selected, e.g. if a data type is marked as `unique` you do not need to generate an explicit constraint for this anymore.
* Be consistent! If a class has an explicit `id` or similar field, always use that field when expressing constraints, not the element's location in a container. You cannot assume that its order in a container matches this field, since that order may be non-deterministic to your solver tool.
* To find elements in a collection with specific characteristics, you can use free variables and assumptions instead. Here are a few examples:

Puzzle condition: "Bob is the person who owns a dog"
Constraint:
```
bob = nondet(solution.people)
assume(bob.name == "Bob")
assert bob.pet == "dog"
```

Puzzle condition: "The coffee drinker is taller than the tea drinker"
Constraint:
```
coffee_drinker = nondet(solution.people)
assume(coffee_drinker.drink == "coffee")
tea_drinker = nondet(solution.people)
assume(tea_drinker.drink == "tea")
assert coffe_drinker.height > tea_drinker.height
```

The validation function signature must look as follows:

```
def validate(solution: Solution) -> None
    # Your constraints belong here...
```

Now generate the conditions necessary to check that a solution to the puzzle is correct! Make sure that you consider every condition stated in the puzzle!"
"""

# Kicks off the data structure generation by the model.
_DATA_STRUCTURE_MESSAGE: str = """Here is the puzzle:
```
{}
```
"""

# Instructs the model to format the solver solution according to the requested format
_FORMAT_MESSAGE: str = """Your logic solver tool produced the following solution:
```
{solution}
```

Now convert it to the expected output format:
```
{output_format}
```"""

# Instructs the model on how to generate a solution data structure.
_SYSTEM_PROMPT: str = """You are an expert puzzle solving agent with access to a propositional logic solver tool that has a convenient interface optimised for you. Your boss has given you a logic puzzle that he thinks can be mapped to your logic solver tool. You must solve this puzzle in two steps:

1. Define the data type for a valid puzzle solution
2. Define the logical constraints for a valid puzzle solution


I will walk you through these steps one by one. Do not attempt to solve the puzzle or a following step before I tell you to do so. We will now begin with the first step: Define the data type for a valid puzzle solution.

Your solver tool allows you to specify the output solution type as Python classes, with a few additional features:

* Just like in SQL, each field can be marked as unique, meaning no two instances of the class can have the same value, e.g.: `id: Unique[int]`
* Each field can have a value constraint assigned to it, such that only these values are allowed, e.g.: `id: Domain[int, range(1, 11)]` allows id values between 1 (inclusive) and 11 (exclusive), or `name: Domain[str, \"John\", \"Jane\", \"Peter\"]` allows only the strings \"John\", \"Jane\", or \"Peter\". 
* The `list` type allows for a second type argument specifying the size, e.g.: `list[int, 10]`.

Here is an example that uses these features in combination:
```
class Row:
    id: Unique[Domain[int, range(1, 11)]]
    name: Unique[Domain[str, \"John\", \"Jane\", \"Peter\"]]
    music: Unique[Domain[str, \"Jazz\", \"Rock\", \"Pop\"]]

class Table:
    rows: list[Row, 10]
```

Always constrain data types according to all the information you can identify in the puzzle text. This is critical for solving the puzzle.

In order to automatically validate the puzzle solution, your data structure will eventually need to be converted to JSON in the following format, so keep that in mind when deciding on your data structure:
```
{}
```

Now specify the type of a valid solution using this syntax."""

# Sent if the solver was unable to find a solution.
_UNSAT_MESSAGE: str = (
    "Your constraints are contradictory and thus the solver could not find a solution. Please review them and try to spot the error, we will go through the step of generating the `def validate(solution: Solution) -> None` function again."
)


class CBMCSearchEngineStrategy(EngineStrategy):
    """
    Solves search-based problems using a CBMC back-end.
    """

    def __init__(
        self, logger_factory: Callable[[str], Logger], task: str, output_format: str
    ) -> None:
        """
        Args:
            logger_factory (Callable[[str], Logger]): Log configuration.
            task (str): Search-based problem to solve.
            output_format (str): Output format expected by the user.
        """
        self.__logger: Logger = logger_factory(__name__)
        self.__task: str = task
        self.__output_format: str = output_format

    @property
    def constraints_prompt(self) -> list[str]:
        return [_CONSTRAINTS_MESSAGE]

    @property
    def data_structure_prompt(self) -> str:
        return _SYSTEM_PROMPT.format(self.__output_format)

    def generate_solver_constraints(
        self, module: Module, metadata: Optional[MetadataWrapper]
    ) -> str:
        return LogicPyCHarnessGenerator.generate(module)

    def generate_solver_invocation_command(self, solver_input_file: str) -> list[str]:
        return [
            "cbmc",
            "-D__CPROVER",
            "--json-ui",
            "--trace",
            solver_input_file,
        ]

    def get_format_prompt(self, solution: str) -> Optional[str]:
        return _FORMAT_MESSAGE.format(
            solution=solution, output_format=self.__output_format
        )

    def parse_solver_output(
        self, exit_code: int, stdout: str, stderr: str
    ) -> Tuple[SolverOutcome, Optional[str]]:
        if exit_code == 10:
            cbmc_json_output: Any = loads(stdout)
            parsed_output: str = CBMCSearchEngineStrategy.__parse_cbmc_solution(
                cbmc_json_output
            )
            return SolverOutcome.SUCCESS, parsed_output

        if exit_code != 0:
            return SolverOutcome.FATAL, None

        self.__logger.error(
            "Unsatisfiable constraint, suggest model should attempt to repair constraints..."
        )
        return SolverOutcome.RETRY, None

    @property
    def python_code_prefix(self) -> str:
        return ""

    @property
    def retry_prompt(self) -> str:
        return _UNSAT_MESSAGE

    @property
    def solver_input_file_suffix(self) -> str:
        return ".c"

    @property
    def system_prompt(self) -> str:
        return _DATA_STRUCTURE_MESSAGE.format(self.__task)

    @staticmethod
    def __cbmc_value_to_string(
        string_builder: StringIO, cbmc_json_value: Any, indent: str
    ) -> None:
        """
        Formats a CBMC C output expression as a Python-ish expression, such
        that the model can easily interpret the answer by the solver.

        Args:
            string_builder (StringIO): Output to which we write the result
            expression.
            cbmc_json_value (Any): CBMC JSON output to format.
            indent (str): Current indentation prefix. Used when recursively
            invoking this method for nested members.
        """
        next_indent: str = indent + "  "
        if "members" in cbmc_json_value:
            string_builder.write(f"{indent}{{\n")
            is_first = True
            for member in cbmc_json_value["members"]:
                name: str = member["name"]
                if name.startswith("$pad"):
                    continue

                if not is_first:
                    string_builder.write(f",\n")
                is_first = False

                string_builder.write(f"{next_indent}{name}: ")
                CBMCSearchEngineStrategy.__cbmc_value_to_string(
                    string_builder, member["value"], next_indent
                )
            string_builder.write(f"\n{indent}}}")
        elif "elements" in cbmc_json_value:
            string_builder.write(f"[\n")
            is_first = True
            for element in cbmc_json_value["elements"]:
                if not is_first:
                    string_builder.write(f",\n")
                is_first = False

                CBMCSearchEngineStrategy.__cbmc_value_to_string(
                    string_builder, element["value"], next_indent
                )
            string_builder.write(f"\n{indent}]")
        elif "data" in cbmc_json_value:
            value: str = cbmc_json_value["data"]
            if cbmc_json_value["type"] == "const char *":
                string_builder.write(f'"{value}"')
            else:
                string_builder.write(value)

    @staticmethod
    def __parse_cbmc_solution(cbmc_output: Any) -> str:
        """
        Extracts the solution counterexample from a CBMC JSON trace, formatted
        as a Pyton-ish expression to make it easier for the model to interpret.

        Args:
            cbmc_output (Any): CBMC JSON output.
        Returns:
            Python-ish expression equivalent to solution output struct in CBMC
            trace.
        """
        output_step: Any = [
            step
            for message in cbmc_output
            if "result" in message
            for result in message["result"]
            if result["status"] == "FAILURE"
            for step in result["trace"]
            if step["stepType"] == "output"
        ][0]
        value: Any = output_step["values"][0]

        string_builder = StringIO()
        CBMCSearchEngineStrategy.__cbmc_value_to_string(string_builder, value, "")
        return string_builder.getvalue()

import argparse
from dataclasses import dataclass, field, replace
import mako.template
import os
import yaml
from collections import defaultdict
import urllib.parse

import dacite
from typing import List, Optional, Dict, Any
import json

from common.general import write, ensure_directory_exists, asdict_without_nones, singleton
from common.hierarchical_logger import hlog, htrack
from benchmark.metrics.statistic import Stat
from benchmark.runner import RunSpec
from benchmark.metrics.metric_name import MetricName
from benchmark.augmentations.perturbation_description import PERTURBATION_WORST
from proxy.models import ALL_MODELS, get_model

"""
Reads the output of the benchmark runs and produces:
- JSON files for the frontend
- Tables for the paper

Usage:

    venv/bin/benchmark-summarize --suite <Name of the suite>

"""

SCHEMA_YAML_PATH: str = "src/proxy/static/schema.yaml"


@dataclass(frozen=True)
class Run:
    """ Represents a run with spec and stats. """

    # Directory name of the run (used by frontend to find the actual instances to load)
    run_path: str

    # Run spec for the run
    run_spec: RunSpec

    # Statistics for the run
    stats: List[Stat]


@dataclass(frozen=True)
class Field:
    """
    Represents a field in a specification (e.g., `temperature` for the adapter,
    or `exact_match` for metrics or `typos` for perturbations.
    """

    # Internal name (usually no spaces, etc.)
    name: str

    # What is displayed to the user
    display_name: Optional[str] = None

    # What is displayed to the user (e.g., in a table header)
    short_display_name: Optional[str] = None

    # Description of the field
    description: Optional[str] = None

    def get_short_display_name(self):
        return self.short_display_name or self.display_name or self.name


@dataclass(frozen=True)
class MetricNameMatcher:
    """
    The schema file specifies information about what metrics we want to specify,
    but it doesn't specify full `MetricName`s.  Instead, it specifies enough
    information in a `MetricNameMatcher` to pull out the relevant
    `MetricName`s.
    """

    # Name of the metric
    name: str

    # Which data split to report numbers on (e.g., TEST_SPLIT)
    split: str

    # Which sub split to report numbers on (e.g., toxic, non-toxic)
    sub_split: Optional[str] = None

    # Which perturbation to show (e.g., robustness)
    perturbation_name: Optional[str] = None

    def matches(self, metric_name: MetricName) -> bool:
        if self.name != metric_name.name or self.split != metric_name.split or self.sub_split != metric_name.sub_split:
            return False

        metric_perturbation_name = metric_name.perturbation and metric_name.perturbation.name
        if self.perturbation_name != metric_perturbation_name:
            return False

        # If there is a perturbation, only return the worst
        if metric_name.perturbation and metric_name.perturbation.computed_on != PERTURBATION_WORST:
            return False
        return True

    def substitute(self, environment: Dict[str, str]) -> "MetricNameMatcher":
        return MetricNameMatcher(
            name=mako.template.Template(self.name).render(**environment),
            split=mako.template.Template(self.split).render(**environment),
            perturbation_name=mako.template.Template(self.perturbation_name).render(**environment)
            if self.perturbation_name is not None
            else None,
        )


@dataclass(frozen=True)
class MetricGroup(Field):
    """
    A list of metrics (which are presumably logically grouped).
    """

    metrics: List[MetricNameMatcher] = field(default_factory=list)


@dataclass(frozen=True)
class ScenarioGroup(Field):
    """
    Defines information about how a scenario group (really a list of runs that
    share the same scenario) are displayed.
    """

    # What groups (by name) to show
    metric_groups: List[str] = field(default_factory=list)

    # Defines variables that are substituted in any of the metrics
    environment: Dict[str, str] = field(default_factory=dict)


@dataclass
class Schema:
    """Specifies information about what to display."""

    # Adapter fields (e.g., temperature)
    adapter: List[Field]

    # Information about each field
    metrics: List[Field]

    # Information about each perturbation
    perturbations: List[Field]

    # Group the metrics
    metric_groups: List[MetricGroup]

    # Group the scenarios
    scenario_groups: List[ScenarioGroup]

    def __post_init__(self):
        self.name_to_metric = {metric.name: metric for metric in self.metrics}
        self.name_to_perturbation = {perturbation.name: perturbation for perturbation in self.perturbations}
        self.name_to_metric_group = {metric_group.name: metric_group for metric_group in self.metric_groups}


@dataclass(frozen=True)
class Cell:
    # Semantic value (that can be used for sorting)
    value: Any

    # Optionally, if we want to render things specially (floating points to 3 decimal points)
    display_value: Optional[str] = None

    # Detailed description if hover over the cell
    description: Optional[str] = None

    # If we click on the link for this cell, it takes us somewhere
    href: Optional[str] = None


@dataclass(frozen=True)
class Table:
    title: str
    header: List[Cell]
    rows: List[List[Cell]]


def get_unique_stat_by_matcher(stats: List[Stat], matcher: MetricNameMatcher) -> Optional[Stat]:
    """Return the single stat that matches."""
    matching_stats = [stat for stat in stats if matcher.matches(stat.name)]
    if len(matching_stats) == 0:
        return None
    # TODO: remove this check once we dedupe metrics
    if len(matching_stats) > 1:
        hlog(f"WARNING: There are more than one matching stat: {matching_stats}")
        return matching_stats[0]
    return singleton(matching_stats)


def get_benchmarking_url(params: Dict[str, str]) -> str:
    return "benchmarking.html?" + urllib.parse.urlencode(params)


class Summarizer:
    """Summarize the benchmark results in JSON files to be displayed in the UI."""

    def __init__(self, run_suite_path: str):
        self.run_suite_path: str = run_suite_path

    def read_schema(self):
        hlog(f"Reading schema from {SCHEMA_YAML_PATH}...")
        with open(SCHEMA_YAML_PATH) as f:
            raw = yaml.safe_load(f)
            self.schema = dacite.from_dict(Schema, raw)

    def read_run(self, run_path: str) -> Run:
        """Load the `Run` object from `run_path`."""

        with open(os.path.join(run_path, "run_spec.json")) as f:
            run_spec = dacite.from_dict(RunSpec, json.load(f))

        with open(os.path.join(run_path, "stats.json")) as f:
            stats = [dacite.from_dict(Stat, raw) for raw in json.load(f)]

        return Run(run_path=run_path, run_spec=run_spec, stats=stats,)

    @htrack(None)
    def read_runs(self):
        """ Load the corresponding runs for the run specs in run_specs.json. """

        run_specs_path: str = os.path.join(self.run_suite_path, "run_specs.json")
        if not os.path.exists(run_specs_path):
            hlog(f"Summarizer won't run because {run_specs_path} doesn't exist yet. This is expected in a dry run.")
            return []

        self.runs: List[Run] = []
        with open(run_specs_path) as f:
            raw_run_specs = json.load(f)
        for raw_run_spec in raw_run_specs:
            run_spec = dacite.from_dict(RunSpec, raw_run_spec)
            run_path: str = os.path.join(self.run_suite_path, run_spec.name)

            run_spec_path: str = os.path.join(run_path, "run_spec.json")
            stats_path: str = os.path.join(run_path, "stats.json")

            if os.path.exists(run_spec_path) and os.path.exists(stats_path):
                run = self.read_run(run_path)
                self.runs.append(run)
            else:
                hlog(f"WARNING: {run_path} doesn't have run_spec.json or stats.json, skipping")

        # Build mapping from scenario group (e.g., natural_qa) and model (e.g., openai/davinci) to list of runs
        self.group_model_to_runs: Dict[str, Dict[str, List[Run]]] = defaultdict(lambda: defaultdict(list))
        self.group_scenario_model_to_runs: Dict[str, Dict[str, List[Run]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list))
        )
        for run in self.runs:
            for group in run.run_spec.groups:
                model = run.run_spec.adapter_spec.model
                # Assume it is sensible to shard by scenario arguments.
                scenario = ", ".join(f"{k}: {v}" for k, v in run.run_spec.scenario_spec.args.items())

                self.group_model_to_runs[group][model].append(run)
                self.group_scenario_model_to_runs[group][scenario][model].append(run)

    @htrack(None)
    def check_metrics_defined(self):
        """Check that all the metrics that appear in stats are defined."""
        # Compute all metric names that were encountered
        metric_name_to_run_spec_names: Dict[str, List[str]] = defaultdict(list)
        for run in self.runs:
            for stat in run.stats:
                metric_name_to_run_spec_names[stat.name.name].append(run.run_spec.name)

        with open(SCHEMA_YAML_PATH) as f:
            metrics = yaml.safe_load(f)["metrics"]
            defined_metric_names = set(entry["name"] for entry in metrics)

        for metric_name, run_spec_names in metric_name_to_run_spec_names.items():
            if metric_name not in defined_metric_names:
                hlog(
                    f"WARNING: metric name {metric_name} undefined in {SCHEMA_YAML_PATH} "
                    f"but appears in {len(run_spec_names)} run specs, including {run_spec_names[0]}"
                )

    def write_models(self):
        write(
            os.path.join(self.run_suite_path, "models.json"),
            json.dumps(list(map(asdict_without_nones, ALL_MODELS)), indent=2),
        )

    def write_runs(self):
        write(
            os.path.join(self.run_suite_path, "runs.json"),
            json.dumps(list(map(asdict_without_nones, self.runs)), indent=2),
        )

    def write_tables_to_latex(self, tables: List[Table], save_path: str, skip_blank_columns=True):
        ensure_directory_exists(save_path)
        for table in tables:
            table_name = table.title.replace(" ", "_").replace("/", "_")
            columns_shown = list(range(len(table.header)))
            # TODO: should we skip blank columns even earlier?
            if skip_blank_columns:
                columns_shown = [i for i in columns_shown if not all(row[i].value is None for row in table.rows)]
            latex = "\\begin{table}[htp]\n"
            latex += "\\begin{tabular}{l" + "r" * (len(columns_shown) - 1) + "}\n"
            latex += "\\toprule\n"
            latex += " & ".join(str(table.header[i].value) for i in columns_shown) + " \\\\\n"
            latex += "\\midrule\n"
            for row in table.rows:
                latex += " & ".join(str(row[i].display_value or row[i].value or "") for i in columns_shown) + " \\\\\n"
            latex += "\\bottomrule\n"
            latex += "\\end{tabular}\n"
            latex += "\\caption{Results for group " + table.title + "}\n"
            latex += "\\label{fig:" + table_name + "}\n"
            latex += "\\end{table}\n"
            write(os.path.join(save_path, f"{table_name}.tex"), latex.replace("%", "\\%"))

    def create_index_table(self) -> Table:
        header = [
            Cell("Scenario"),
            Cell("Description"),
            Cell("# models"),
        ]
        rows = []
        for group in self.schema.scenario_groups:
            rows.append(
                [
                    Cell(group.display_name, href=get_benchmarking_url({"group": group.name})),
                    Cell(group.description),
                    Cell(len(self.group_model_to_runs[group.name])),
                ]
            )
        return Table(title="Overview of results", header=header, rows=rows,)

    def create_cell(self, runs: List[Run], matcher: MetricNameMatcher) -> Cell:
        """Use the metric name identified by `matcher` to pull out the stats
        from `runs` and return a representation of the average."""
        if len(runs) == 0:
            return Cell(None)

        aggregate_stat: Optional[Stat] = None
        for run in runs:
            stat = get_unique_stat_by_matcher(run.stats, matcher)
            if stat is None:
                hlog(f"WARNING: {matcher} doesn't match {run.run_spec.name}")
                continue  # TODO: probably should make a note that stats are missing
            stat = stat.take_mean()  # Collapse to a single point

            if aggregate_stat is None:
                aggregate_stat = replace(stat)
            else:
                aggregate_stat.merge(stat)

        if aggregate_stat is None:
            return Cell(None)
        value = aggregate_stat.mean
        display_value = round(value, 3) if value else value
        description = aggregate_stat.bare_str()  # Show more information
        return Cell(value=value, display_value=display_value, description=description)

    def create_group_table(
        self, title: str, scenario_group: ScenarioGroup, model_to_runs: Dict[str, List[Run]], link_to_runs: bool
    ) -> Table:
        """
        Create a table for a scenario_group (natural_qa) where each row is a
        model and columns are constructed based on metrics.
        Here's how the columns are constructed.  For each metric group:
        - Take the cross product over the list of metric names (e.g.,
          exact_match) and perturbation names (e.g., typos) defined by the
          metric group.
        - In some cases, the scenario group will supply the metric names.
        """

        # Figure out what the columns of the table are.
        # Create header (cells to display) and the list of metric name filters
        # (to pull out information later).
        header: List[Cell] = []
        matchers: List[MetricNameMatcher] = []
        header.append(Cell("Model"))
        for metric_group_name in scenario_group.metric_groups:
            metric_group = self.schema.name_to_metric_group[metric_group_name]
            for metric in metric_group.metrics:
                matcher = metric.substitute(scenario_group.environment)
                header_field = self.schema.name_to_metric[matcher.name]

                header_name = header_field.get_short_display_name()
                description = header_field.display_name + ": " + header_field.description

                if matcher.perturbation_name is not None:
                    perturbation_field = self.schema.name_to_perturbation[matcher.perturbation_name]
                    header_name += " (" + perturbation_field.get_short_display_name() + ")"
                    description += (
                        "\n- Perturbation "
                        + perturbation_field.display_name
                        + ": "
                        + (perturbation_field.description or "???")
                    )

                header.append(Cell(header_name, description=description))
                matchers.append(matcher)

        # Populate the contents of the table
        rows = []
        for model_name, runs in model_to_runs.items():
            model = get_model(model_name)
            # Link to all the runs under this model
            if link_to_runs:
                run_spec_names = [run.run_spec.name for run in runs]
                href = get_benchmarking_url({"runSpec": "|".join(run_spec_names)})
            else:
                href = None
            rows.append(
                [Cell(model.display_name, href=href)] + [self.create_cell(runs, matcher) for matcher in matchers]
            )

        return Table(title=title, header=header, rows=rows)

    def write_groups(self):
        """
        Each group selcts out a set of runs.

        For each group, output:
        - Main table (model x columns): each row aggregate over all runs that match the (group, model).
        - Table for each scenario spec.
        """

        # Write out index file with all the groups and basic stats
        write(
            os.path.join(self.run_suite_path, "groups.json"),
            json.dumps(asdict_without_nones(self.create_index_table())),
        )

        # Write out a separate JSON for each group
        groups_path = os.path.join(self.run_suite_path, "groups")
        ensure_directory_exists(groups_path)
        for group in self.schema.scenario_groups:
            tables = []

            # Show the table aggregating over all the scenarios in this group (e.g., babi-1, babi-2, etc.)
            if len(self.group_scenario_model_to_runs[group.name]) > 1:
                tables.append(
                    self.create_group_table(
                        title=f"{group.display_name}",
                        scenario_group=group,
                        model_to_runs=self.group_model_to_runs[group.name],
                        link_to_runs=False,
                    )
                )

            # Show the table per scenario
            for scenario in self.group_scenario_model_to_runs[group.name]:
                scenario_display_name = f"{group.display_name} / {scenario}"
                tables.append(
                    self.create_group_table(
                        title=scenario_display_name,
                        scenario_group=group,
                        model_to_runs=self.group_scenario_model_to_runs[group.name][scenario],
                        link_to_runs=True,
                    )
                )

            # Write it!
            write(
                os.path.join(groups_path, group.name + ".json"), json.dumps(list(map(asdict_without_nones, tables))),
            )
            self.write_tables_to_latex(tables, os.path.join(groups_path, "latex"))


@htrack(None)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-o", "--output-path", type=str, help="Where the benchmarking output lives", default="benchmark_output"
    )
    parser.add_argument(
        "--suite", type=str, help="Name of the suite this run belongs to (default is today's date).", required=True,
    )
    args = parser.parse_args()

    # Output JSON files summarizing the benchmark results which will be loaded in the web interface
    summarizer = Summarizer(run_suite_path=os.path.join(args.output_path, "runs", args.suite))
    summarizer.read_schema()
    summarizer.read_runs()
    summarizer.write_models()
    summarizer.write_runs()
    summarizer.write_groups()
    summarizer.check_metrics_defined()
from mythic_container.MythicCommandBase import *
import json


class LsArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        self.args = [
            CommandParameter(
                name="path",
                type=ParameterType.String,
                description="Path of file or folder to list (default: current directory)",
                parameter_group_info=[ParameterGroupInfo(required=False)],
                default_value=".",
            )
        ]

    async def parse_arguments(self):
        if len(self.command_line) > 0:
            if self.command_line[0] == '{':
                temp_json = json.loads(self.command_line)
                if "host" in temp_json:
                    # Came from file browser UI click
                    self.add_arg("path", temp_json["path"] + "/" + temp_json["file"])
                else:
                    self.add_arg("path", temp_json.get("path", "."))
            else:
                self.add_arg("path", self.command_line.strip())
        else:
            self.add_arg("path", ".")


class LsCommand(CommandBase):
    cmd = "ls"
    needs_admin = False
    help_cmd = "ls [/path/to/directory] — use 'shell ls -la' for flags"
    description = "List directory contents for the file browser. For raw ls with flags, use: shell ls -la"
    version = 2
    author = "@0xNirvana"
    argument_class = LsArguments
    attackmapping = ["T1083"]
    supported_ui_features = ["file_browser:list"]
    browser_script = BrowserScript(script_name="ls", author="@its_a_feature_", for_new_ui=True)

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )
        response.DisplayParams = taskData.args.get_arg("path")
        return response

    async def process_response(self, task: PTTaskMessageAllData, response: any) -> PTTaskProcessResponseMessageResponse:
        resp = PTTaskProcessResponseMessageResponse(TaskID=task.Task.ID, Success=True)
        return resp

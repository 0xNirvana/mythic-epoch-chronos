from mythic_container.MythicCommandBase import *


class CdArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        self.args = [
            CommandParameter(
                name="path",
                type=ParameterType.String,
                description="Directory to change to",
                parameter_group_info=[ParameterGroupInfo(required=True)],
            )
        ]

    async def parse_arguments(self):
        if len(self.command_line) == 0:
            raise ValueError("Must supply a directory path")
        self.add_arg("path", self.command_line.strip())


class CdCommand(CommandBase):
    cmd = "cd"
    needs_admin = False
    help_cmd = "cd <directory>"
    description = "Change working directory"
    version = 1
    author = "@mythic"
    argument_class = CdArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )
        return response

    async def process_response(self, task: PTTaskMessageAllData, response: any) -> PTTaskProcessResponseMessageResponse:
        resp = PTTaskProcessResponseMessageResponse(TaskID=task.Task.ID, Success=True)
        return resp

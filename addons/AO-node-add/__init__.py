bl_info = {
    "name": "AO Node Add",
    "author": "Claude",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "Shader Editor > Add > Group / N-panel > glTF",
    "description": "Vytvoří node group 'glTF Material Output' se vstupem 'Occlusion' pro export glTF (AO baking)",
    "category": "Node",
}

import bpy
from bpy.types import Operator, Panel
from bpy.utils import register_class, unregister_class

GROUP_NAME = "glTF Material Output"
INPUT_NAME = "Occlusion"


def get_or_create_gltf_output_group():
    """Vrátí existující node group 'glTF Material Output', nebo ji vytvoří."""
    group = bpy.data.node_groups.get(GROUP_NAME)
    if group is not None:
        return group

    group = bpy.data.node_groups.new(GROUP_NAME, "ShaderNodeTree")

    # Blender 4.x: nové rozhraní přes node_tree.interface
    if hasattr(group, "interface"):
        socket = group.interface.new_socket(
            name=INPUT_NAME,
            in_out="INPUT",
            socket_type="NodeSocketFloat",
        )
        socket.default_value = 1.0
        socket.min_value = 0.0
        socket.max_value = 1.0
    else:
        # Fallback pro Blender < 4.0
        socket = group.inputs.new("NodeSocketFloat", INPUT_NAME)
        socket.default_value = 1.0
        socket.min_value = 0.0
        socket.max_value = 1.0

    # Group Input node uvnitř skupiny - jen aby byla vidět struktura
    group_input = group.nodes.new("NodeGroupInput")
    group_input.location = (-200, 0)

    return group


class NODE_OT_add_gltf_material_output(Operator):
    """Přidá node group 'glTF Material Output' do aktivního shader editoru"""
    bl_idname = "node.add_gltf_material_output"
    bl_label = "AO Node Add"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (
            space is not None
            and space.type == "NODE_EDITOR"
            and getattr(space, "edit_tree", None) is not None
        )

    def execute(self, context):
        space = context.space_data
        tree = space.edit_tree

        group = get_or_create_gltf_output_group()

        node = tree.nodes.new("ShaderNodeGroup")
        node.node_tree = group
        node.name = GROUP_NAME
        node.label = GROUP_NAME
        node.location = (0, 0)

        for n in tree.nodes:
            n.select = False
        node.select = True
        tree.nodes.active = node

        self.report({"INFO"}, f"Node group '{GROUP_NAME}' byla přidána.")
        return {"FINISHED"}


def menu_func(self, context):
    self.layout.operator(
        NODE_OT_add_gltf_material_output.bl_idname,
        text="AO Node Add",
        icon="NODE",
    )


class NODE_PT_gltf_material_output_panel(Panel):
    """Panel v N-sidebaru shader editoru pro rychlé přidání node group"""
    bl_label = "AO Node Add"
    bl_idname = "NODE_PT_gltf_material_output"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "glTF"

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == "ShaderNodeTree"

    def draw(self, context):
        layout = self.layout
        layout.operator(NODE_OT_add_gltf_material_output.bl_idname, icon="NODE")


classes = (
    NODE_OT_add_gltf_material_output,
    NODE_PT_gltf_material_output_panel,
)


def register():
    for cls in classes:
        register_class(cls)
    bpy.types.NODE_MT_add.append(menu_func)


def unregister():
    bpy.types.NODE_MT_add.remove(menu_func)
    for cls in reversed(classes):
        unregister_class(cls)


if __name__ == "__main__":
    register()

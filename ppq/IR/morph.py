from typing import Any, List

import numpy as np
import torch
from ppq.core import (DataType, TargetPlatform,
                      convert_any_to_python_primary_type)
from ppq.IR.quantize import DeviceSwitchOP
from ppq.scheduler import value_tracing_pattern

from .base.command import (GraphCommand, GraphCommandType,
                           ReplaceOperationCommand, ReplaceVariableCommand)
from .base.graph import Operation, Variable
from .processer import GraphCommandProcesser


class GraphReplacer(GraphCommandProcesser):
    def process(self, command: GraphCommand) -> Any:
        if command.command_type == GraphCommandType.REPLACE_OP:
            assert isinstance(command, ReplaceOperationCommand), \
                'Use ReplaceOperationCommand instead of GraphCommand'
            return self.replace_op(command.op_name, command.replace_to)
        if command.command_type == GraphCommandType.REPLACE_VAR:
            assert isinstance(command, ReplaceVariableCommand), \
                'Use ReplaceOperationCommand instead of GraphCommand'
            return self.replace_var(command.op_name, command.replace_to)

    def replace_op(self, op_name: str, replace_to: Operation):
        if op_name not in self._graph.operations:
            raise KeyError(f'Opeartion {op_name} is not in current graph')
        operation = self._graph.operations[op_name]

        replace_to.inputs.clear()
        replace_to.inputs.extend(operation.inputs)
        for input_var in operation.inputs:
            dest_idx = input_var.dest_ops.index(operation)
            input_var.dest_ops[dest_idx] = replace_to

        replace_to.outputs.clear()
        replace_to.outputs.extend(operation.outputs)
        for output_var in operation.outputs:
            output_var.source_op = replace_to

        replace_to.parameters.clear()
        replace_to.parameters.extend(operation.parameters)

        self._graph.operations[op_name] = replace_to

    def replace_var(self, var_name: str, replace_to: Variable):
        if var_name not in self._graph.variables:
            raise KeyError(f'Variable {var_name} is not in current graph')
        variable = self._graph.variables[var_name]

        replace_to.dest_ops.clear()
        replace_to.dest_ops.extend(variable.dest_ops)
        for dest_op in replace_to.dest_ops:
            dest_idx = dest_op.inputs.index(variable)
            dest_op.inputs[dest_idx] = replace_to

        replace_to.source_op = variable.source_op
        if variable.source_op is not None:
            source_idx = variable.source_op.outputs.index(variable)
            variable.source_op.outputs[source_idx] = replace_to

        self._graph.variables[var_name] = replace_to
        if var_name in self._graph.inputs:
            self._graph.inputs[var_name] = replace_to
        if var_name in self._graph.outputs:
            self._graph.outputs[var_name] = replace_to

    def _acceptable_command_types(self) -> List[GraphCommandType]:
        return [
            GraphCommandType.REPLACE_OP,
            GraphCommandType.REPLACE_VAR,
        ]

class GraphFormatter(GraphCommandProcesser):
    def _acceptable_command_types(self) -> List[GraphCommandType]:
        return [
            GraphCommandType.FORMAT_CLIP,
            GraphCommandType.FORMAT_PAD,
            GraphCommandType.FORMAT_GATHER,
            GraphCommandType.FORMAT_CAST,
            GraphCommandType.FORMAT_INT64_CONSTANT,
            GraphCommandType.DELETE_ISOLATED,
            GraphCommandType.REPLACE_SUB,
            GraphCommandType.FORMAT_PARAMETERS,
            GraphCommandType.FORMAT_CONSTANT_INPUT
        ]

    def process(self, command: GraphCommand) -> Any:
        if command.command_type == GraphCommandType.FORMAT_CLIP:
            return self.format_clip()
        if command.command_type == GraphCommandType.FORMAT_PAD:
            return self.format_pad()
        if command.command_type == GraphCommandType.FORMAT_GATHER:
            return self.format_gather()
        if command.command_type == GraphCommandType.FORMAT_CAST:
            return self.format_cast()
        if command.command_type == GraphCommandType.DELETE_ISOLATED:
            return self.delete_isolated()
        if command.command_type == GraphCommandType.FORMAT_INT64_CONSTANT:
            return self.format_int64_constant()
        if command.command_type == GraphCommandType.REPLACE_SUB:
            return self.replace_substarction()
        if command.command_type == GraphCommandType.FORMAT_PARAMETERS:
            return self.format_parameter_variables()
        if command.command_type == GraphCommandType.FORMAT_CONSTANT_INPUT:
            return self.format_constant_input()

    def format_pad(self) -> None:
        """
            对于不同的模型格式, pad 算子将有两种不同的输入格式：
            for different models, possibly Pad op has the following input formats
                1. pads 参数由第二个输入变量给出 
                   pads parameter is given by the second input variable
                2. pads 参数被放置于 operation.attribute 中
                   pads parameter is set in attribute
            此函数统一 pad 算子行为：所有 pad 算子的 pads 参数均由 operation.attribute 给出
            this func unifies behaviors of Pad op: pads paramter will always given in
            attribute
            同时当 padding mode 设置为 constant 时，pads 将存在一个用来确定 padding value 的值
            存在该值时，该函数返回 ValueError
            when the padding mode is set to constant, its constant input will be used as
            padding value
        """
        interested_ops = []
        for _, operation in self.graph.operations.items():
            if operation.type == 'Pad': interested_ops.append(operation)
        for operation in interested_ops:
            assert isinstance(operation, Operation)
            padding_value = operation.attributes.get('pads_value', 0)
            padding_mode = operation.attributes.get('mode', 'constant')
            if padding_mode == 'constant' and len(operation.inputs) == 3:
                pads_variable = operation.inputs[1]
                pads_constant_op = pads_variable.source_op
                padding_value = pads_constant_op.attributes['value']
                self.__delete_constant_input(operation, 1)
            if len(operation.inputs) > 1:
                # here exist a pattern: constant -> pad
                pads_variable = operation.inputs[1]
                pads_constant_op = pads_variable.source_op
                pads = pads_constant_op.attributes['value']
                self.__delete_constant_input(operation, 1)
                operation.attributes['pads'] = convert_any_to_python_primary_type(pads)
            if padding_mode == 'constant': operation.attributes['pads_value'] = padding_value

    def format_clip(self) -> None:
        """
            对于不同的模型格式, clip 算子将有两种不同的输入格式：
            for different models, possibly clip op has the following input formats
                1. min, max 参数由 第二、第三个输入变量给出
                   min, max parameter will be given by the second and third input variable
                2. min, max 参数由 attribute 给出
                   min, max parameter will be given by the attribute
            此函数统一 clip 算子行为：所有 clip 算子的 min, max 参数均由 operation.attribute 给出
            this func unifies behaviors of clip op: min, max parameter will be given in 
            attribute
            针对可能存在的 min, max 为空的情况，将其直接置为 2 << 30（保证处理后非空）

            当 min, max 参数由 第二、第三个输入变量给出时，其中一个为空时直接返回 ValueError
            ValueError will be raised when any of min, max parameters is null
        """
        interested_ops = []
        for _, operation in self.graph.operations.items():
            if operation.type == 'Clip': interested_ops.append(operation)
        for operation in interested_ops:
            assert isinstance(operation, Operation)
            if len(operation.inputs) == 3:
                min_constant_op, max_constant_op = [var.source_op for var in operation.inputs[1:]]
                min = convert_any_to_python_primary_type(min_constant_op.attributes['value'])
                max = convert_any_to_python_primary_type(max_constant_op.attributes['value'])
                self.__delete_constant_input(operation, 2)
                self.__delete_constant_input(operation, 1)
            elif len(operation.inputs) == 1:
                min = operation.attributes.get('min', - 2 << 30)
                max = operation.attributes.get('max', + 2 << 30)
            else:
                raise ValueError(f'Expect clip has 1 or 3 inputs, while {len(operation.inputs)} was given')
            operation.attributes['min'] = min
            operation.attributes['max'] = max

    def format_gather(self) -> None:
        """
            gather op 的参数 index 可能由 input variable 给出
            但 index 参数不可以被量化，同时后端运算需要其作为Python 原生类型
            因此将其转移到 gather op 的属性上。
            index parameter of gather op can be given by input variable,
            however, it can't be quantized, thus we transfer index parameter
            to attribute of gather op

            axis is set to 0 when it's not given
            gather op 的参数 axis 可能不存在，此时强制植入 0 作为 axis 参数
        """
        interested_ops = []
        for _, operation in self.graph.operations.items():
            if operation.type == 'Gather': interested_ops.append(operation)
        for operation in interested_ops:
            assert isinstance(operation, Operation)
            if len(operation.inputs) == 2:
                index_op = operation.inputs[1].source_op
                if index_op.type == 'Constant':
                    index = index_op.attributes['value']
                    self.__delete_constant_input(operation, 1)
                    operation.attributes['gather_index'] = convert_any_to_python_primary_type(index)
            if 'axis' not in operation.attributes:
                operation.attributes['axis'] = 0

            if 'indices' in operation.attributes:
                operation.attributes['gather_index'] = operation.attributes['indices']
                operation.attributes.pop('indices')

    def format_cast(self) -> None:
        """
            cast op 的参数 to 默认为 int，使用该函数将其封装为 ppq.core.DataType
        """
        interested_ops = []
        for _, operation in self.graph.operations.items():
            assert isinstance(operation, Operation)
            if operation.type == 'Cast': interested_ops.append(operation)
        for operation in interested_ops:
            assert isinstance(operation, Operation)
            assert 'to' in operation.attributes
            operation.attributes['to'] = DataType.convert_from_numpy(operation.attributes['to'])

    def format_int64_constant(self) -> None:
        """
            convert all int64 constants to int32, check if direct dtype cast is available
            将所有 int64 的 Constant 转换为 int32
            将检查所有 Constant value, 如果 value 范围在 int32 表示范围内则执行转换。
        """
        for operation in self.graph.operations.values():
            if operation.type == 'Constant':
                assert 'value' in operation.attributes
                value = operation.attributes['value']

                assert isinstance(value, torch.Tensor)
                if value.dtype != torch.int64: continue

                pvalue = convert_any_to_python_primary_type(value)
                check = [0xFFFFFFFF > v >= -0xFFFFFFFF for v in pvalue]

                if all(check): value = value.int()

    def format_constant_input(self) -> None:
        """
        部分部署平台不支持 Constant Op，在这种情况下我们使用这个 pass 把 Constant Op 的输入切换成 parameter variable 的形式
        some backend platform doesn't support Constant Op, we use this pass to replace it by forcing its value to 
        be a parameter variable
        """
        constant_ops = []
        for operation in self.graph.operations.values():
            if operation.type == 'Constant':
                assert len(operation.outputs) == 1, (
                    f"Constant Operation {operation.name} has more than 1 output, is there a network parsing error?")
                constant_ops.append(operation)

        for operation in constant_ops:
            assert isinstance(operation, Operation)
            output_var = operation.outputs[0]

            constant_value = operation.attributes['value']
            output_var.value = constant_value
            # force output variable to a parameter.
            output_var._is_parameter = True
            
            operation.outputs.clear()
            output_var.source_op = None
            self.graph.delete_operation(op_name=operation.name)

    def delete_isolated(self):
        blacklist = [None]
        while len(blacklist) > 0:
            blacklist = []
            # delete all operations which are not links to a valid graph output
            for op in self.graph.operations.values():
                if len(self.graph.get_downstream_operations(op)) == 0:
                    output_names = [var.name for var in op.outputs]
                    if all([name not in self.graph.outputs for name in output_names]):
                        blacklist.append(op)

            for op in blacklist:
                for var in op.outputs:
                    self.graph.delete_variable(var, force_delete=True)
                self.graph.delete_operation(op, force_delete=True)

    def format_parameter_variables(self) -> None:
        vars = []
        for var in self.graph.variables.values():
            if var.is_parameter and len(var.dest_ops) > 1:
                # found parameter with multiple destination operations
                # split parameter variable
                vars.append(var)

        for var in vars:
            assert isinstance(var, Variable)
            for idx, dest_op in enumerate(var.dest_ops.copy()):
                # create variables
                sub_var = Variable(
                    name=var.name + '_' + str(idx), 
                    value=var.value, is_parameter=True,
                    dest_ops=[dest_op], source_op=None)
                self.graph.append_variable(sub_var)
                
                # replace original variable with splited one.
                dest_op.inputs[dest_op.inputs.index(var)] = sub_var
                var.dest_ops.remove(dest_op)

            # pop variable from graph
            self.graph.delete_variable(var.name)

    def replace_substarction(self) -> None:
        substractions = []
        for operation in self.graph.operations.values():
            if operation.type == 'Sub':
                substractions.append(operation)
        
        for operation in substractions:
            assert isinstance(operation, Operation)
            subtractor = operation.inputs[-1].source_op
            substractor_var = operation.inputs[-1]

            # create a neg operation
            neg_op = Operation(name=subtractor.name + '_neg', op_type='Neg', attributes={})
            self.graph.append_operation(neg_op)
            
            # create related variables
            neg_var = Variable(name=subtractor.name + '_neg_1', dest_ops=[operation], source_op=neg_op)

            # link var to op
            neg_op.inputs.append(substractor_var)
            neg_op.outputs.append(neg_var)
            
            operation.inputs.remove(substractor_var)
            operation.inputs.append(neg_var)

            substractor_var.dest_ops.remove(operation)
            substractor_var.dest_ops.append(neg_op)
            
            # add var to graph
            self.graph.append_variable(neg_var)
            
            # replace sub to add
            operation._type = 'Add'

    def __delete_constant_input(self, op: Operation, input_idx: int):
        op_name = op.name
        if op_name not in self._graph.operations:
            raise KeyError(f'Operation {op_name} not in current graph.')
        operation = self._graph.operations[op_name]
        assert input_idx < len(operation.inputs), 'Trying to delete an out-of-range input variable, '\
                f'has graph been manully changed? Error at Opeartion {op_name}, input_idx: {input_idx}'
        input_var = operation.inputs[input_idx]
        if input_var.source_op.type != 'Constant':
            raise ValueError(f'Trying to delete an non-const input, '\
                f'Error at Opeartion {op_name}, inputs[{input_idx}]')
        input_var.dest_ops.pop(input_var.dest_ops.index(operation))
        operation.inputs.pop(input_idx)
        if len(input_var.dest_ops) == 0:
            self.graph.delete_variable(input_var.name)
            self.graph.delete_operation(input_var.source_op.name)

class GraphMerger(GraphCommandProcesser):
    """
    Graph Merger implements all graph fusion related functions.
    """
    def _acceptable_command_types(self) -> List[GraphCommandType]:
        return [
            # add more extensions in the future 
            GraphCommandType.FUSE_CONV_BN
        ]

    def process(self, command: GraphCommand) -> Any:
        if command.command_type == GraphCommandType.FUSE_CONV_BN:
            return self.fuse_conv_bn()

    def fuse_conv_bn(self):
        conv_bns = []
        for operation in self.graph.operations.values():
            downstream_ops = self.graph.get_downstream_operations(operation)
            if len(downstream_ops) == 1 and downstream_ops[0].type == 'BatchNormalization':
                conv_bns.append(operation, downstream_ops[0])

        for conv, bn in conv_bns:
            assert isinstance(conv, Operation) and isinstance(bn, Operation)
            
            if conv.num_of_parameters == 1:
                conv_weight = conv.parameters[0].value
                conv_bias   = np.zeros((conv_weight.shape[0]), dtype='f')
            else:
                conv_weight, conv_bias = conv.parameters[: 2]

            assert len(bn.parameters) == 4, 'BatchNorm should have 4 parameters, namely alpha, beta, mean, var'
            alpha = bn.parameters[0].value
            beta  = bn.parameters[1].value
            mean  = bn.parameters[2].value
            var   = bn.parameters[3].value
            epsilon = bn.attributes.get('epislon', 1e-5)

            # calculate new weight and bias
            new_scale = alpha / np.sqrt(var + epsilon)
            new_weight = conv_weight.transpose(*range(1, len(conv_weight.shape)), 0) * new_scale
            new_weight = new_weight.transpose(len(conv_weight.shape)-1, *range(len(conv_weight.shape)-1))
            new_bias = alpha * (conv_bias - mean) / np.sqrt(var + epsilon) + beta
            # create new op and variable
            new_conv_op = Operation(conv.name, 'Conv', attributes=conv.attributes.copy())
            new_weight = Variable(conv.name + '_weight_', new_weight, True, [new_conv_op])
            new_bias = Variable(conv.name + '_bias_', new_bias, True, [new_conv_op])
            # replace
            new_conv_op.inputs.extend([conv.inputs[0], new_weight, new_bias])
            conv.inputs[0].dest_ops[conv.inputs[0].dest_ops.index(conv)] = new_conv_op
            new_conv_op.outputs.extend(bn.outputs[:1])
            new_conv_op.outputs[0].source_op = new_conv_op

            for var in conv.inputs[1:]:
                var.dest_ops.pop(var.dest_ops.index(conv))
                if len(var.dest_ops) <= 0 and not (var.name in self.graph.outputs or var.name in self.graph.inputs):
                    if not var.source_op is None:
                        var.source_op.outputs.pop(var.source_op.outputs.index(var))
                    self.graph.variables.pop(var.name)
            for var in bn.inputs:
                var.dest_ops.pop(var.dest_ops.index(bn))
                if len(var.dest_ops) <= 0 and not (var.name in self.graph.outputs or var.name in self.graph.inputs):
                    if not var.source_op is None:
                        var.source_op.outputs.pop(var.source_op.outputs.index(var))
                    self.graph.variables.pop(var.name)
            # remove old ops and insert new conv
            self.graph.operations.pop(conv.name)
            self.graph.operations.pop(bn.name)
            self.graph.variables[new_weight.name] = new_weight
            self.graph.variables[new_bias.name] = new_bias
            self.graph.operations[new_conv_op.name] = new_conv_op

class GraphDeviceSwitcher(GraphCommandProcesser):
    """
    Graph Device Switcher insert necessary switcher operation for
        graph split and device dispatching.
    
    See also ppq scheduler for more information.
    
    All SOI operations are supposed to be executed on cpu.
        while other operations are supposed to be executed on cuda.
        Therefore switching operation will be inserted between SOI operations and fp32(quant) operations.
        to transfer cuda tensor to cpu tensor, vice versa.

    However some operations receive SOI input(cpu tensor) naturally, such as reshape, slice, etc.
    PPQ uses a tracing function for judging whether it is necessary to insert a
        switcher between operations like that.
        
    Before invoking this class, all operations must have been dispatched by a dispatcher.

    Args:
        GraphCommandProcesser ([type]): [description]
    """
    def insert_switcher(self):
        """
        Insert all necessary switchers into current graph.
            Before invoking this function, all operations must have been dispatched by a dispatcher.
        
        THIS IS AN NOT-REENTRANT FUNCTION!
        """
        def can_pass_shape(from_op: Operation, to_op: Operation) -> bool:
            if to_op.platform == TargetPlatform.SHAPE_OR_INDEX: return True
            else: return not value_tracing_pattern(from_where=from_op, to_where=to_op)
        
        soi_ops = []
        for operation in self.graph.operations.values():
            if operation.platform == TargetPlatform.SHAPE_OR_INDEX:
                soi_ops.append(operation)

        for operation in soi_ops:
            assert isinstance(operation, Operation)
            for var in operation.outputs:
                if all([can_pass_shape(operation, op) for op in var.dest_ops]): continue
                # else there is at least one opeartion needs a device converter.

                if all([not can_pass_shape(operation, op) for op in var.dest_ops]):
                    boundary_op = DeviceSwitchOP(name=var.name + '_Switcher')
                    self._graph.insert_operation_on_var(inserting_op=boundary_op, var=var.name)
                    boundary_op.platform = TargetPlatform.FP32
                else:
                    for dest_op in var.dest_ops:
                        if can_pass_shape(operation, dest_op): continue
                        boundary_op = DeviceSwitchOP(name=operation.name + '_' + dest_op.name)
                        self._graph.insert_operation_btw(inserting_op=boundary_op, up_op=operation, down_op=dest_op)
                        boundary_op.platform = TargetPlatform.FP32
            
            for var in operation.inputs:
                source_op = var.source_op
                # TODO refine here.
                if source_op is None: continue
                if source_op.platform != TargetPlatform.SHAPE_OR_INDEX and not source_op.is_soi_generator:
                    boundary_op = DeviceSwitchOP(name=source_op.name + '_' + operation.name)
                    self._graph.insert_operation_btw(inserting_op=boundary_op, up_op=source_op, down_op=operation)
                    boundary_op.platform = TargetPlatform.SHAPE_OR_INDEX

    def remove_switcher(self):
        """
        remove all switchers from current graph.
        """
        removing_collection = []
        for operation in self.graph.operations.values():
            if operation.type == 'PPQDeviceSwitch':
                removing_collection.append(operation)

        for op in removing_collection:
            self.graph.remove_operation(removing_op=op)

    def _acceptable_command_types(self) -> List[GraphCommandType]:
        return [
            GraphCommandType.INSERT_SWITCHER,
            GraphCommandType.REMOVE_SWITCHER
        ]

    def process(self, command: GraphCommand) -> Any:
        if command.command_type == GraphCommandType.INSERT_SWITCHER:
            return self.insert_switcher()
        if command.command_type == GraphCommandType.REMOVE_SWITCHER:
            return self.remove_switcher()
